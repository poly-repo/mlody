"""Starlark-like Evaluator for .mlody files.

This module provides a sandboxed Python environment for executing user-defined
scripts with a `.mlody` extension.  It is designed to safely evaluate
configuration or definition files in a controlled manner, similar to build
systems like Bazel that use Starlark.

Core Concepts:
- **Sandboxing**: Scripts are executed with a limited, explicitly-defined set
  of globally available functions and types, specified in ``SAFE_BUILTINS``.
  This prevents scripts from accessing arbitrary I/O or other unsafe
  operations.  **Note:** The sandbox is best-effort.  It is intended to
  discourage accidental misuse, not to enforce a hard security boundary
  against a determined attacker.

- **``PYTHON_SPECIFIC_BUILTINS``**: An intentional design element exposed as
  the ``python`` variable inside ``.mlody`` scripts.  It acts as a clearly-
  demarcated namespace for Python constructs that are valid Python but not
  valid Starlark (e.g. ``python.hasattr``, ``python.getattr``).  The explicit
  ``python.`` prefix makes such usages easy to audit: ``grep python\\.``
  surfaces every script location that needs attention when migrating away from
  Python-specific prototype behaviour.

- **``Evaluator`` Class**: The main entry point.  An instance manages the state
  of the evaluation, including loaded files and registered objects.

- **``load()`` Statement**: Scripts can import symbols from other ``.mlody``
  files using a custom ``load()`` function injected into the sandbox.  It
  supports four path forms:

  - ``@ROOT//package/path:file.mlody`` — anchored to a registered root name
    (the root must have been registered via ``builtins.register("root", ...)``
    before the ``load()`` is reached).
  - ``//path/to/file.mlody`` — repo-root-absolute.
  - ``:sibling.mlody`` — sibling of the current file.
  - ``relative/path.mlody`` — relative to the current file.

- **Registration**: Scripts communicate results back to the host system via
  ``builtins.register(kind: str, thing: Struct)``.  The ``Evaluator`` instance
  collects registered objects in its internal state (e.g. ``self.roots``),
  accessible after evaluation completes.
"""

import ast
import builtins
import functools
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import uuid_utils

from common.python.starlarkish.core.struct import Struct, struct

_log = logging.getLogger(__name__)
_ENTITY_DESCRIPTOR_TYPE_NAMES = {
    "root": "mlody-root",
    "value": "mlody-value",
    "task": "mlody-task",
    "action": "mlody-action",
}
_MISSING = object()


def _declared_child_specs(value_type: object) -> tuple[object, ...]:
    """Return merged declared child specs for a type, including legacy aliases."""
    attrs = getattr(value_type, "attributes", None)
    direct_fields = getattr(value_type, "fields", None)
    attrs_fields = attrs.get("fields") if isinstance(attrs, dict) else None
    direct_virtual = getattr(value_type, "virtual_attributes", None)
    attrs_virtual = attrs.get("virtual_attributes") if isinstance(attrs, dict) else None

    specs: list[object] = []
    seen: set[str] = set()
    for spec in list(direct_fields or attrs_fields or []):
        name = getattr(spec, "name", None)
        if isinstance(name, str) and name not in seen:
            specs.append(spec)
            seen.add(name)
    for spec in list(direct_virtual or attrs_virtual or []):
        name = getattr(spec, "name", None)
        if isinstance(name, str) and name not in seen:
            specs.append(spec)
            seen.add(name)
    return tuple(specs)


def _validate_loads_at_top(script_content: str, file_path: Path) -> None:
    """Raise SyntaxError if any load() call appears after a non-load statement."""
    try:
        tree = ast.parse(script_content, filename=str(file_path))
    except SyntaxError:
        return  # let exec() produce the real error

    past_loads = False  # True once we see a non-load statement
    for i, stmt in enumerate(tree.body):
        # Allow module docstring as the very first statement
        if (
            i == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue

        is_load = (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "load"
        )
        if is_load and past_loads:
            raise SyntaxError(
                f"load() at line {stmt.lineno} must appear before all other "
                f"code in {file_path}"
            )
        if not is_load:
            past_loads = True


class Named(Protocol):
    """A protocol for objects with a 'name' attribute."""

    name: str


def _sandbox_type(obj: object) -> str:
    """Starlark-compatible type() — returns a type-name string, never a type object.

    Python's built-in type() is a class-creation mechanism and a well-known
    exec-sandbox escape.  This wrapper returns plain strings (matching Starlark's
    type() semantics) so it can be exposed safely as the 'type' builtin inside
    .mlody scripts.
    """
    if obj is None:
        return "NoneType"
    if isinstance(obj, bool):  # bool precedes int — bool is a subclass of int
        return "bool"
    if isinstance(obj, int):
        return "int"
    if isinstance(obj, float):
        return "float"
    if isinstance(obj, str):
        return "string"
    if isinstance(obj, list):
        return "list"
    if isinstance(obj, dict):
        return "dict"
    if isinstance(obj, tuple):
        return "tuple"
    if isinstance(obj, Struct):
        return "struct"
    return "unknown"


def _parse_astropy_unit(text: str) -> object:
    """Parse an Astropy unit string into a unit object."""
    from astropy import units as u

    return u.Unit(text)


def _uuid7_string() -> str:
    """Return a random UUID v7 string for Starlark-side materializers."""
    return str(uuid_utils.uuid7())


# Python-specific builtins that are not part of the Starlark standard.
# These will be exposed under a `python` object.
PYTHON_SPECIFIC_BUILTINS = struct(
    hasattr=builtins.hasattr,
    getattr=builtins.getattr,
    parse_astropy_unit=_parse_astropy_unit,
    uuid7=_uuid7_string,
    round=builtins.round,
    sum=builtins.sum,
    Any=Any,
    Callable=Callable,
    re=re,
    hashlib=hashlib,
    os=os,
)

# A curated list of safe built-ins to expose to user scripts.
# This aligns with the "deny-by-default" security policy.
# NOTE: Python's built-in `type` is NOT exposed here.  Instead, 'type' maps to
# `_sandbox_type`, a safe string-returning wrapper with Starlark semantics.
# `isinstance` is safe: scripts can only test against classes already in the sandbox.
# Exception classes (ValueError, TypeError, NotImplementedError) are safe to raise/catch.
SAFE_BUILTINS: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
    "abs": builtins.abs,
    "all": builtins.all,
    "any": builtins.any,
    "bool": builtins.bool,
    "callable": builtins.callable,
    "dict": builtins.dict,
    "enumerate": builtins.enumerate,
    "float": builtins.float,
    "int": builtins.int,
    "len": builtins.len,
    "list": builtins.list,
    "max": builtins.max,
    "min": builtins.min,
    "print": builtins.print,
    "range": builtins.range,
    "repr": builtins.repr,
    "reversed": builtins.reversed,
    "set": builtins.set,
    "sorted": builtins.sorted,
    "isinstance": builtins.isinstance,
    "str": builtins.str,
    "struct": struct,
    "tuple": builtins.tuple,
    "type": _sandbox_type,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "NotImplementedError": NotImplementedError,
    "zip": builtins.zip,
    "None": None,
    "True": True,
    "False": False,
    "Struct": Struct,
    "python": PYTHON_SPECIFIC_BUILTINS,
}


@dataclass
class Builtins:
    register: Callable[[str, Named], None]
    lookup: Callable[[str, str], Any]
    ctx: Struct
    inject: Callable[[str, Any], None]


class Evaluator:
    """
    The core engine that prepares a sandboxed environment and executes a user script.
    """

    def __init__(
        self,
        root: Path,
        init_files: list[Path] | None = None,
        print_fn: Callable[..., None] = builtins.print,
        extra_ctx: Struct | None = None,
        line_range_extractor: Callable[
            [Path, str], dict[tuple[str, str], tuple[int, int]]
        ]
        | None = None,
        resolve_hook: Callable[[str], Any] | None = None,
        force_hook: Callable[[object], Any] | None = None,
        setf_hook: Callable[..., Any] | None = None,
    ) -> None:
        self.loaded_files: set[Path] = set()
        self._eval_stack: list[Path] = []
        self.root_path: Path = root
        self.roots: dict[str, Named] = dict()
        self.types: dict[str, Named] = dict()
        self.locations: dict[str, Named] = dict()
        self.representations: dict[str, Named] = dict()
        self.values: dict[str, Named] = dict()
        self._roots_by_name: dict[str, Named] = {}
        self._types_by_name: dict[str, Named] = {}
        self._locations_by_name: dict[str, Named] = {}
        self._representations_by_name: dict[str, Named] = {}
        self._values_by_name: dict[str, Named] = {}
        self.actions: dict[str, Named] = {}
        self._actions_by_name: dict[str, Named] = {}
        self.tasks: dict[str, Named] = {}
        self._tasks_by_name: dict[str, Named] = {}
        self.implementations: dict[str, Named] = {}
        self._implementations_by_name: dict[str, Named] = {}
        self.build_refs: dict[str, Named] = {}
        self._build_refs_by_name: dict[str, Named] = {}
        self.executors: dict[str, Named] = {}
        self._executors_by_name: dict[str, Named] = {}
        self.all: dict[str, Named] = {}
        for _pname in ["integer", "string", "bool", "float"]:
            _sentinel: Named = Struct(  # type: ignore[assignment]
                kind="type", type=_pname, name=_pname, attributes={}, _allowed_attrs={}
            )
            self.types[_pname] = _sentinel  # bare key — no file ctx at init time
            self._types_by_name[_pname] = _sentinel
            #            self.all[f"type/{_pname}"] = _sentinel
            self.all[("type", None, _pname)] = _sentinel
        self._module_globals: dict[Path, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        # Override print in the sandbox so callers can suppress stdout writes
        # (e.g. the LSP server, which uses stdout as its JSON-RPC transport).
        self._print_fn = print_fn
        # Extra fields merged into builtins.ctx for every file (e.g. workspace/run info).
        self._extra_ctx = extra_ctx
        # Optional hook: maps (kind, name) -> (start_line, end_line) per file.
        self._line_range_extractor = line_range_extractor
        self._file_ranges: dict[Path, dict[tuple[str, str], tuple[int, int]]] = {}
        self._resolve_hook = resolve_hook
        self._force_hook = force_hook
        self._setf_hook = setf_hook
        if init_files:
            for init_file in init_files:
                path_to_load = init_file
                if not path_to_load.is_absolute():
                    path_to_load = self.root_path / path_to_load
                self._execute_file(path_to_load)

    def _decorate_source_range(self, value: Struct) -> Struct:
        source_range_type = self._types_by_name.get("mlody-source-range")
        if source_range_type is None or getattr(value, "_entity_type", None) is source_range_type:
            return value
        return Struct(**value.as_mapping(), _entity_type=source_range_type)

    def _materialized_child_specs(
        self,
        kind: str,
        fields: dict[str, Any],
    ) -> tuple[tuple[str, object], ...]:
        specs: list[tuple[str, object]] = []
        seen: set[str] = set()

        if kind == "value":
            for spec in _declared_child_specs(fields.get("type")):
                name = getattr(spec, "name", None)
                if isinstance(name, str) and callable(getattr(spec, "materializer", None)):
                    specs.append((name, spec))
                    seen.add(name)

        descriptor_name = _ENTITY_DESCRIPTOR_TYPE_NAMES.get(kind)
        descriptor = self._types_by_name.get(descriptor_name) if descriptor_name is not None else None
        for spec in _declared_child_specs(descriptor):
            name = getattr(spec, "name", None)
            if (
                isinstance(name, str)
                and name not in seen
                and callable(getattr(spec, "materializer", None))
            ):
                specs.append((name, spec))
                seen.add(name)

        return tuple(specs)

    def _make_materialized_child_value(
        self,
        *,
        field_name: str,
        field_spec: object,
        owner_provider: Callable[[], Struct | None],
        label_prefix: str,
    ) -> Struct:
        cached_value: dict[str, object] = {"value": _MISSING}
        child_type = getattr(field_spec, "type", None)
        materializer = getattr(field_spec, "materializer", None)
        assert callable(materializer)

        def _materialize(_value: object) -> object:
            existing = cached_value["value"]
            if existing is not _MISSING:
                return existing
            owner = owner_provider()
            if owner is None:
                raise RuntimeError(f"materialized field {field_name!r} owner not initialised")
            cached_value["value"] = materializer(owner)
            return cached_value["value"]

        return Struct(
            kind="value",
            type=child_type,
            location=Struct(
                kind="location",
                type="virtual",
                name="virtual",
                materializer=_materialize,
            ),
            label=f"{label_prefix}.{field_name}" if label_prefix else field_name,
            _lineage=[],
            name=field_name,
        )

    def decorate_registered_value(self, kind: str, value: Any) -> Any:  # pyright: ignore[reportExplicitAny]
        if not isinstance(value, Struct):
            return value

        fields = dict(value.as_mapping())
        changed = False
        descriptor_name = _ENTITY_DESCRIPTOR_TYPE_NAMES.get(kind)
        if descriptor_name is not None:
            descriptor = self._types_by_name.get(descriptor_name)
            if descriptor is not None and fields.get("_entity_type") is not descriptor:
                fields["_entity_type"] = descriptor
                changed = True

        source_range_value = fields.get("_source_range")
        if isinstance(source_range_value, Struct):
            decorated_source_range = self._decorate_source_range(source_range_value)
            if decorated_source_range is not source_range_value:
                fields["_source_range"] = decorated_source_range
                changed = True

        materialized_specs = self._materialized_child_specs(kind, fields)
        owner_snapshot: Struct | None = None
        owner_name = str(fields.get("name", ""))
        for field_name, field_spec in materialized_specs:
            if field_name in fields:
                continue
            fields[field_name] = self._make_materialized_child_value(
                field_name=field_name,
                field_spec=field_spec,
                owner_provider=lambda: owner_snapshot,
                label_prefix=owner_name,
            )
            changed = True

        if not changed:
            return value
        owner_snapshot = Struct(**fields)
        return owner_snapshot

    def _make_source_range_struct(
        self,
        *,
        rel_file: Path,
        start_line: int,
        end_line: int,
    ) -> Struct:
        fields: dict[str, Any] = {
            "kind": "mlody-source-range",
            "filepath": str(rel_file),
            "start_line": start_line,
            "end_line": end_line,
        }
        source_range_type = self._types_by_name.get("mlody-source-range")
        if source_range_type is not None:
            fields["_entity_type"] = source_range_type
        return Struct(**fields)

    def _refresh_declared_entity_types(self) -> None:
        registry_mappings: tuple[tuple[dict[Any, Any], str], ...] = (
            (self.roots, "root"),
            (self._roots_by_name, "root"),
            (self.values, "value"),
            (self._values_by_name, "value"),
            (self.tasks, "task"),
            (self._tasks_by_name, "task"),
            (self.actions, "action"),
            (self._actions_by_name, "action"),
        )
        for mapping, kind in registry_mappings:
            for key, value in list(mapping.items()):
                mapping[key] = self.decorate_registered_value(kind, value)

        for key, value in list(self.all.items()):
            if not (isinstance(key, tuple) and len(key) == 3):
                continue
            registered_kind = key[0]
            if isinstance(registered_kind, str) and registered_kind in _ENTITY_DESCRIPTOR_TYPE_NAMES:
                self.all[key] = self.decorate_registered_value(registered_kind, value)

        for module_globals in self._module_globals.values():
            for name, value in list(module_globals.items()):
                kind = getattr(value, "kind", None)
                if isinstance(kind, str) and kind in _ENTITY_DESCRIPTOR_TYPE_NAMES:
                    module_globals[name] = self.decorate_registered_value(kind, value)

    def _register(self, kind: str, thing: Named, ctx: Struct) -> None:
        try:
            rel_file = ctx.file.relative_to(self.root_path)
            _stem = str(rel_file.with_suffix(""))
        except (ValueError, AttributeError):
            _stem = getattr(ctx, "file", Path("unknown")).stem
        key = f"{_stem}:{thing.name}"

        if self._line_range_extractor is not None:
            sr = self._file_ranges.get(ctx.file, {}).get((kind, thing.name))
            if sr is not None and isinstance(thing, Struct):
                thing = Struct(
                    **thing.as_mapping(),
                    _source_range=self._make_source_range_struct(
                        rel_file=rel_file,
                        start_line=sr[0],
                        end_line=sr[1],
                    ),
                )

        thing = self.decorate_registered_value(kind, thing)

        if kind == "root":
            self.roots[key] = thing
            self._roots_by_name[thing.name] = thing
        elif kind == "type":
            self.types[key] = thing
            self._types_by_name[thing.name] = thing
        elif kind == "location":
            self.locations[key] = thing
            self._locations_by_name[thing.name] = thing
        elif kind == "representation":
            self.representations[key] = thing
            self._representations_by_name[thing.name] = thing
        elif kind == "value":
            self.values[key] = thing
            self._values_by_name[thing.name] = thing
        elif kind == "action":
            self.actions[key] = thing
            self._actions_by_name[thing.name] = thing
        elif kind == "task":
            self.tasks[key] = thing
            self._tasks_by_name[thing.name] = thing
        elif kind == "implementation":
            self.implementations[key] = thing
            self._implementations_by_name[thing.name] = thing
        elif kind == "build_ref":
            self.build_refs[key] = thing
            self._build_refs_by_name[thing.name] = thing
        elif kind == "executor":
            self.executors[key] = thing
            self._executors_by_name[thing.name] = thing
        else:
            raise ValueError(
                f"Unknown registration kind {kind!r}. Supported kinds: 'root', 'type', 'location', 'representation', 'value', 'action', 'task', 'implementation', 'build_ref', 'executor'."
            )
        self.all[(kind, _stem, thing.name)] = thing
        if kind == "type":
            self._refresh_declared_entity_types()
        _log.debug("Registered %r as %s", key, kind)

    def _lookup(self, kind: str, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        # Strip leading ':' so local-reference syntax (":foo") resolves like "foo".
        if name.startswith(":"):
            name = name[1:]
        if kind == "type":
            if name not in self._types_by_name:
                raise NameError(
                    f"No type {name!r}. Available: {sorted(self._types_by_name)}"
                )
            return self._types_by_name[name]
        elif kind == "root":
            if name not in self._roots_by_name:
                raise NameError(
                    f"No root {name!r}. Available: {sorted(self._roots_by_name)}"
                )
            return self._roots_by_name[name]
        elif kind == "location":
            if name not in self._locations_by_name:
                raise NameError(
                    f"No location {name!r}. Available: {sorted(self._locations_by_name)}"
                )
            return self._locations_by_name[name]
        elif kind == "representation":
            if name not in self._representations_by_name:
                raise NameError(
                    f"No representation {name!r}. Available: {sorted(self._representations_by_name)}"
                )
            return self._representations_by_name[name]
        elif kind == "value":
            if name not in self._values_by_name:
                raise NameError(
                    f"No value {name!r}. Available: {sorted(self._values_by_name)}"
                )
            return self._values_by_name[name]
        elif kind == "action":
            if name not in self._actions_by_name:
                raise NameError(
                    f"No action {name!r}. Available: {sorted(self._actions_by_name)}"
                )
            return self._actions_by_name[name]
        elif kind == "task":
            if name not in self._tasks_by_name:
                raise NameError(
                    f"No task {name!r}. Available: {sorted(self._tasks_by_name)}"
                )
            return self._tasks_by_name[name]
        elif kind == "implementation":
            if name not in self._implementations_by_name:
                raise NameError(
                    f"No implementation {name!r}. Available: {sorted(self._implementations_by_name)}"
                )
            return self._implementations_by_name[name]
        elif kind == "build_ref":
            if name not in self._build_refs_by_name:
                raise NameError(
                    f"No build_ref {name!r}. Available: {sorted(self._build_refs_by_name)}"
                )
            return self._build_refs_by_name[name]
        elif kind == "executor":
            if name not in self._executors_by_name:
                raise NameError(
                    f"No executor {name!r}. Available: {sorted(self._executors_by_name)}"
                )
            return self._executors_by_name[name]
        else:
            raise ValueError(
                f"Unknown lookup kind {kind!r}. Supported: 'root', 'type', 'location', 'representation', 'value', 'action', 'task', 'implementation', 'build_ref', 'executor'."
            )

    def _load(
        self,
        path: str,
        *symbols: str,
        current_file: Path,
        caller_globals: dict[str, Any],
    ) -> None:  # pyright: ignore[reportExplicitAny]
        """
        Implementation of the Starlark-like `load()`:
         - path: file path (relative, //-absolute, or @ROOT//-anchored)
         - current_file: Path of the file that invoked load()
         - caller_globals: the globals dict of the caller (so we can inject symbols)
         - *symbols: optional names to import; if omitted, import all public globals

        Supported path forms::

            @ROOT//package/path:file.mlody   root-anchored: ROOT is a registered root name
            //path/to/file.mlody             repo-root-absolute
            :sibling.mlody                   sibling of the current file
            relative/path.mlody              relative to current file
        """
        if path.startswith("@"):
            # @ROOT//package/path:file.mlody
            if "//" not in path:
                raise ValueError(
                    f"load() path {path!r} starting with '@' must contain '//'"
                )
            slashslash = path.index("//")
            root_name = path[1:slashslash]
            rest = path[slashslash + 2 :]  # strip leading "//"
            if ":" not in rest:
                raise ValueError(
                    f"load() path {path!r} must contain ':' after '@ROOT//package'"
                )
            colon = rest.index(":")
            package = rest[:colon]
            filename = rest[colon + 1 :]
            if root_name not in self._roots_by_name:
                raise NameError(
                    f"load() references unknown root @{root_name!r}; "
                    f"available: {sorted(self._roots_by_name)}"
                )
            root_obj = self._roots_by_name[root_name]
            root_rel_path: str = getattr(root_obj, "path", "")  # pyright: ignore[reportAny]
            if not isinstance(root_rel_path, str):
                raise TypeError(
                    f"Root @{root_name!r} 'path' field must be a string, "
                    f"got {type(root_rel_path).__name__!r}"
                )
            root_abs = (self.root_path / root_rel_path.lstrip("/")).resolve()
            if package:
                target_path = (root_abs / package / filename).resolve()
            else:
                target_path = (root_abs / filename).resolve()
        elif path.startswith("//"):
            # Resolve //... to root_path
            target_path = (self.root_path / path[2:]).resolve()
        elif path.startswith(":"):
            # resolve :... relative to current file
            target_path = (current_file.parent / path[1:]).resolve()
        else:
            # resolve relative to current file
            target_path = (current_file.parent / path).resolve()

        # Execute (or fetch cached execution) of target file; returns its globals dict
        target_globals = self._execute_file(target_path)

        # Decide which symbols to import
        if symbols:
            names_to_copy = symbols
        else:
            # default: copy all public names (no leading underscore),
            # but skip __builtins__ and 'load' to avoid clobbering caller environment.
            names_to_copy = [
                name
                for name in target_globals.keys()
                if not name.startswith("_") and name not in ("__builtins__", "load")
            ]

        for name in names_to_copy:
            if name not in target_globals:
                raise NameError(f"module {path} has no symbol {name!r}")
            if name in ("__builtins__", "load"):
                continue
            caller_globals[name] = target_globals[name]

    def _execute_file(self, file_path: Path) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """Executes a single .mlody file and returns the globals dict."""
        if file_path in self._eval_stack:
            stack_copy = self._eval_stack + [file_path]
            raise ImportError(
                f"Circular import detected: {' -> '.join(map(str, stack_copy))}"
            )

        if file_path in self.loaded_files:
            # If already loaded, return the cached globals dict from the first execution.
            return self._module_globals.get(file_path, {})

        self._eval_stack.append(file_path)
        try:
            self.loaded_files.add(file_path)

            _log.debug("Evaluating %s", file_path)

            with open(file_path, "r", encoding="utf-8") as f:
                script_content = f.read()

            _validate_loads_at_top(script_content, file_path)

            if self._line_range_extractor is not None:
                self._file_ranges[file_path] = self._line_range_extractor(
                    file_path, script_content
                )

            # Prepare sandbox globals.  Spread SAFE_BUILTINS and override "print"
            # with the instance-level print_fn so that callers (e.g. the LSP server)
            # can suppress sandbox stdout writes without mutating the shared
            # module-level constant.
            sandbox_globals: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
                "__builtins__": {**SAFE_BUILTINS, "print": self._print_fn},
                "__MLODY__": True,
            }

            ctx_kwargs: dict[str, Any] = {
                "directory": file_path.parent,
                "file": file_path,
            }  # pyright: ignore[reportExplicitAny]
            if self._extra_ctx is not None:
                ctx_kwargs.update(self._extra_ctx.as_mapping())
            ctx_struct = Struct(**ctx_kwargs)

            # The register callable computes ctx at call time so that ctx.directory
            # reflects the file whose exec() is currently in progress — not the file
            # where the callable was created.  This matters when a loaded helper
            # function (e.g. root() in builtins.mlody) calls builtins.register on
            # behalf of the file that invoked it.
            def _register_for_file(kind: str, thing: Named) -> None:
                current_file = self._eval_stack[-1] if self._eval_stack else file_path
                call_ctx = Struct(
                    **{  # pyright: ignore[reportExplicitAny]
                        "directory": current_file.parent,
                        "file": current_file,
                        **(
                            self._extra_ctx.as_mapping()
                            if self._extra_ctx is not None
                            else {}
                        ),
                    }
                )
                self._register(kind, thing, ctx=call_ctx)

            def _inject_into_sandbox(name: str, value: Any) -> None:  # pyright: ignore[reportExplicitAny]
                # Inject into the file that is CURRENTLY EXECUTING (top of eval
                # stack), not necessarily the file where this closure was created.
                # This mirrors _register_for_file's use of self._eval_stack[-1]
                # so that typedef() injects the factory into the calling file's
                # scope even when typedef is a function imported from another file.
                current_file = self._eval_stack[-1] if self._eval_stack else file_path
                target_globals = self._module_globals.get(current_file, sandbox_globals)
                target_globals[name] = value

            builtins_obj = Builtins(
                register=_register_for_file,
                lookup=self._lookup,
                ctx=ctx_struct,
                inject=_inject_into_sandbox,
            )
            sandbox_globals["builtins"] = builtins_obj
            if self._resolve_hook is not None:
                sandbox_globals["resolve"] = self._resolve_hook
            if self._force_hook is not None:
                sandbox_globals["force"] = self._force_hook
            if self._setf_hook is not None:
                sandbox_globals["setf"] = self._setf_hook

            # create a load function that will inject into this sandbox's globals
            load_func = functools.partial(
                self._load, current_file=file_path, caller_globals=sandbox_globals
            )
            sandbox_globals["load"] = load_func

            # Register sandbox_globals BEFORE exec so that _inject_into_sandbox can
            # look up the current file's globals via self._module_globals during execution.
            self._module_globals[file_path] = sandbox_globals

            # Execute the file in its sandbox
            exec(script_content, sandbox_globals)

            return sandbox_globals
        finally:
            self._eval_stack.pop()

    def eval_file(self, entrypoint_file: Path) -> None:
        """
        Evaluates a script and any scripts it loads.

        The results of the evaluation are stored in the evaluator's state
        (e.g., `self.roots`).

        Args:
            entrypoint_file: The path to the root script to execute.
        """
        self._execute_file(entrypoint_file)

    def resolve(self) -> None:
        """Resolution phase: replace string labels in actions/tasks with entity references.

        Must be called after all files have been evaluated (loading phase complete).
        Raises NameError for any unresolvable label.
        """

        def _resolve_value(v: object) -> Named:
            if isinstance(v, str):
                return self._lookup("value", v)
            return v  # type: ignore[return-value]

        def _resolve_action(v: object) -> Named:
            if isinstance(v, str):
                return self._lookup("action", v)
            return v  # type: ignore[return-value]

        # Resolve actions: string labels in inputs/outputs/config → value structs
        for key, entity in list(self.actions.items()):
            fields = dict(entity.as_mapping())
            fields["inputs"] = [_resolve_value(v) for v in fields.get("inputs", [])]
            fields["outputs"] = [_resolve_value(v) for v in fields.get("outputs", [])]
            _config = fields.get("config", [])
            if isinstance(_config, list):
                fields["config"] = [_resolve_value(v) for v in _config]
            new_entity = Struct(**fields)
            self.actions[key] = new_entity
            self._actions_by_name[entity.name] = new_entity
            self.all[f"action/{key}"] = new_entity

        # Resolve tasks: string labels in inputs/outputs/config/action → entity structs
        for key, entity in list(self.tasks.items()):
            fields = dict(entity.as_mapping())
            fields["inputs"] = [_resolve_value(v) for v in fields.get("inputs", [])]
            fields["outputs"] = [_resolve_value(v) for v in fields.get("outputs", [])]
            _config = fields.get("config", [])
            if isinstance(_config, list):
                fields["config"] = [_resolve_value(v) for v in _config]
            fields["action"] = _resolve_action(fields.get("action"))
            new_entity = Struct(**fields)
            self.tasks[key] = new_entity
            self._tasks_by_name[entity.name] = new_entity
            self.all[f"task/{key}"] = new_entity
