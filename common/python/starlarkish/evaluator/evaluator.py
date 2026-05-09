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
  collects registered objects in its internal registry state
  (e.g. ``self.registry.roots.by_key``), accessible after evaluation completes.
"""

import ast
import builtins
import functools
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import uuid_utils

try:
    from mlody.common.struct import (
        is_struct_like as _is_struct_like,
        struct_like_as_mapping as _struct_like_as_mapping,
    )
except ModuleNotFoundError:
    def _is_struct_like(value: object) -> bool:
        return isinstance(value, Struct)

    def _struct_like_as_mapping(value: object) -> Any:
        if isinstance(value, Struct):
            return value.as_mapping()
        raise TypeError(f"expected Struct-like value, got {type(value).__name__}")

from common.python.starlarkish.core.struct import Struct, struct
from common.python.starlarkish.evaluator.registry import Named, RegistryState

_log = logging.getLogger(__name__)
_ENTITY_DESCRIPTOR_TYPE_NAMES = {
    "root": "mlody-root",
    "value": "mlody-value",
    "task": "mlody-task",
    "action": "mlody-action",
}
_MISSING = object()
_LEGACY_REGISTRY_ATTRS = {
    "roots": ("roots", "by_key"),
    "_roots_by_name": ("roots", "by_name"),
    "types": ("types", "by_key"),
    "_types_by_name": ("types", "by_name"),
    "locations": ("locations", "by_key"),
    "_locations_by_name": ("locations", "by_name"),
    "freshnesses": ("freshnesses", "by_key"),
    "_freshnesses_by_name": ("freshnesses", "by_name"),
    "representations": ("representations", "by_key"),
    "_representations_by_name": ("representations", "by_name"),
    "values": ("values", "by_key"),
    "_values_by_name": ("values", "by_name"),
    "actions": ("actions", "by_key"),
    "_actions_by_name": ("actions", "by_name"),
    "tasks": ("tasks", "by_key"),
    "_tasks_by_name": ("tasks", "by_name"),
    "implementations": ("implementations", "by_key"),
    "_implementations_by_name": ("implementations", "by_name"),
    "build_refs": ("build_refs", "by_key"),
    "_build_refs_by_name": ("build_refs", "by_name"),
    "executors": ("executors", "by_key"),
    "_executors_by_name": ("executors", "by_name"),
    "generics": ("generics", "by_key"),
    "_generics_by_name": ("generics", "by_name"),
    "configs": ("configs", "by_key"),
    "_configs_by_name": ("configs", "by_name"),
}


def _wrap_registered_value(kind: str, value: object) -> object:
    try:
        from mlody.common._registered_struct import wrap_registered_struct  # noqa: PLC0415
    except ModuleNotFoundError:
        return value

    return wrap_registered_struct(kind, value)


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


def _parse_quantity_string(text: str, unit: object) -> float:
    """Parse an astropy quantity string and convert to *unit*.

    Returns the magnitude as a Python float.
    Raises ValueError on bad input or incompatible units.
    """
    from astropy import units as u

    try:
        q = u.Quantity(text)
    except Exception as exc:
        raise ValueError(f"Cannot parse {text!r} as a quantity: {exc}") from exc
    try:
        return float(q.to(unit).value)
    except u.UnitConversionError as exc:
        raise ValueError(f"Cannot convert {q.unit} to {unit}: {exc}") from exc


def _format_quantity_string(value: float, unit: object) -> str:
    """Format a numeric magnitude and astropy unit as a quantity string."""
    from astropy import units as u  # noqa: PLC0415

    return str(u.Quantity(value, unit))


def _uuid7_string() -> str:
    """Return a random UUID v7 string for Starlark-side materializers."""
    return str(uuid_utils.uuid7())


def _is_virtual_value_struct(obj: object) -> bool:
    """Return True when *obj* is a typed virtual value Struct."""
    if not isinstance(obj, Struct):
        return False
    if getattr(obj, "kind", None) != "value":
        return False
    loc = getattr(obj, "location", None)
    return (
        loc is not None
        and getattr(loc, "type", None) == "virtual"
        and callable(getattr(loc, "materializer", None))
    )


def _force_virtual_value_struct(obj: object) -> object:
    """Materialize a virtual value Struct, returning all other inputs unchanged."""
    if not _is_virtual_value_struct(obj):
        return obj
    loc = getattr(obj, "location", None)
    assert loc is not None
    materializer = getattr(loc, "materializer", None)
    if materializer is None:
        return obj
    return materializer(obj)


def _looks_like_workspace(obj: object) -> bool:
    """Duck-type check for mlody Workspace objects without importing mlody.core."""
    return (
        hasattr(obj, "_monorepo_root")
        and hasattr(obj, "_workspace_root")
        and hasattr(obj, "root_infos")
    )


def _runtime_json_data(obj: object, *, _seen: set[int] | None = None) -> object:
    """Convert runtime objects to JSON-safe data, forcing virtual children."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if callable(obj) and not isinstance(obj, type):
        return "<callable>"
    if _is_virtual_value_struct(obj):
        return _runtime_json_data(_force_virtual_value_struct(obj), _seen=_seen)

    if _seen is None:
        _seen = set()

    is_container_like = (
        _is_struct_like(obj)
        or isinstance(obj, (dict, list, tuple, set))
        or _looks_like_workspace(obj)
        or hasattr(obj, "__dict__")
    )
    obj_id = id(obj)
    if is_container_like:
        if obj_id in _seen:
            return "<cycle>"
        _seen.add(obj_id)

    try:
        if _is_struct_like(obj):
            result: dict[str, object] = {}
            for key, value in _struct_like_as_mapping(obj).items():
                if key in {"raw", "_entity_type"}:
                    continue
                result[str(key)] = _runtime_json_data(value, _seen=_seen)
            return result
        if isinstance(obj, dict):
            return {
                str(key): _runtime_json_data(value, _seen=_seen)
                for key, value in obj.items()
            }
        if isinstance(obj, (list, tuple, set)):
            return [_runtime_json_data(value, _seen=_seen) for value in obj]
        if _looks_like_workspace(obj):
            return {
                "monorepo_root": str(getattr(obj, "_monorepo_root", "")),
                "workspace_root": str(getattr(obj, "_workspace_root", "")),
                "full_workspace": bool(getattr(obj, "_full_workspace", False)),
                "root_infos": _runtime_json_data(
                    getattr(obj, "root_infos", {}), _seen=_seen
                ),
                "info": _runtime_json_data(getattr(obj, "info", None), _seen=_seen),
            }
        if hasattr(obj, "__dict__"):
            return {
                str(key): _runtime_json_data(value, _seen=_seen)
                for key, value in vars(obj).items()
                if key not in {"raw", "_entity_type", "_evaluator", "evaluator"}
            }
        return repr(obj)
    finally:
        if is_container_like:
            _seen.remove(obj_id)


def _runtime_json_blob(obj: object) -> str:
    """Return a stable, pretty-printed JSON snapshot of a runtime object."""
    return json.dumps(_runtime_json_data(obj), indent=2, sort_keys=True)


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{4,39}$")


def _expand_commit_sha(value: str) -> str:
    """Verify a commit SHA exists in the repo and return its canonical 40-char form.

    Accepts a full 40-char hex string or an abbreviated form (4–39 hex chars);
    both are resolved via ``git rev-parse --verify <sha>^{commit}`` so existence
    and uniqueness are always confirmed.  The repo root is determined by:

    1. ``BUILD_WORKSPACE_DIRECTORY`` env-var (set automatically by ``bazel run``)
    2. ``CWD`` (works when the CLI is invoked from inside the repo)

    Raises ``TypeError`` when *value* is not a hex string, does not exist, or
    is ambiguous.
    """
    if not isinstance(value, str):
        raise TypeError(f"commit-sha must be a string, got {type(value).__name__!r}")
    if not _FULL_SHA_RE.match(value) and not _SHORT_SHA_RE.match(value):
        raise TypeError(
            f"commit-sha must be a hex string (4–40 chars), got {value!r}"
        )
    import git as _git  # noqa: PLC0415

    workspace_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    root = Path(workspace_dir) if workspace_dir else Path.cwd()
    try:
        repo = _git.Repo(root, search_parent_directories=True)
        full = repo.git.rev_parse("--verify", f"{value}^{{commit}}")
    except _git.exc.GitCommandError:
        raise TypeError(
            f"commit SHA {value!r} does not exist or is ambiguous in the repository"
        )
    except Exception as exc:
        raise TypeError(f"Cannot verify commit SHA {value!r}: {exc}") from exc
    if not _FULL_SHA_RE.match(full):
        raise TypeError(
            f"commit SHA {value!r} resolved to unexpected output: {full!r}"
        )
    return full


# Python-specific builtins that are not part of the Starlark standard.
# These will be exposed under a `python` object.
PYTHON_SPECIFIC_BUILTINS = struct(
    hasattr=builtins.hasattr,
    getattr=builtins.getattr,
    parse_astropy_unit=_parse_astropy_unit,
    parse_quantity_string=_parse_quantity_string,
    format_quantity_string=_format_quantity_string,
    runtime_json_blob=_runtime_json_blob,
    uuid7=_uuid7_string,
    round=builtins.round,
    sum=builtins.sum,
    Any=Any,
    Callable=Callable,
    re=re,
    hashlib=hashlib,
    os=os,
    # id() is needed by mm.mlody to key dispatch functions in _GENERIC_NAMES.
    # It is safe to expose: it returns an integer (the memory address of the
    # object), which cannot be used as a sandbox escape.
    id=builtins.id,
    expand_commit_sha=_expand_commit_sha,
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
    "callable": builtins.callable,
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
    # Closed over the Evaluator's _method_registry dict at construction time.
    # Default None so that direct Builtins(...) construction sites that have
    # not been updated yet don't fail with a missing-argument error.
    register_method: Callable[[str, Any], None] | None = None
    get_methods: Callable[[str], list[Any]] | None = None
    # dispatch_method wraps mlody.core.multimethod.dispatch so that mm.mlody's
    # dispatch_fn can call it without a Python-style import (imports are blocked
    # in the sandbox because __import__ is stripped from SAFE_BUILTINS).
    dispatch_method: Callable[[str, Any, list[Any]], Any] | None = None


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
        self.registry = RegistryState()
        # Structure per generic name: {"arity": int | None, "methods": list[Struct]}
        self._method_registry: dict[str, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        for _pname in ["integer", "string", "bool", "float"]:
            _sentinel: Named = Struct(  # type: ignore[assignment]
                kind="type", type=_pname, name=_pname, attributes={}, _allowed_attrs={}
            )
            self.registry.register(
                "type",
                _pname,
                self.decorate_registered_value("type", _sentinel),
            )  # bare key — no file ctx at init time
        self._module_globals: dict[Path, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        # Names injected sandbox-wide: seeded into every new file's sandbox_globals.
        # workspace_loader populates this after evaluating mm.mlody so that `mm`
        # and `defmethod` are available in all subsequent user files without an
        # explicit load().
        self._persistent_injections: dict[str, Any] = {}  # pyright: ignore[reportExplicitAny]
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

    def __getattr__(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        """Expose legacy registry dict names while the rest of the tree migrates."""
        if name == "all":
            return self.registry.all
        registry_attr = _LEGACY_REGISTRY_ATTRS.get(name)
        if registry_attr is None:
            msg = f"{type(self).__name__!r} object has no attribute {name!r}"
            raise AttributeError(msg)
        bucket_name, mapping_name = registry_attr
        return getattr(getattr(self.registry, bucket_name), mapping_name)

    def _decorate_source_range(self, value: Struct) -> Struct:
        source_range_type = self.registry.types.by_name.get("mlody-source-range")
        if (
            source_range_type is None
            or getattr(value, "_entity_type", None) is source_range_type
        ):
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
                if isinstance(name, str) and callable(
                    getattr(spec, "materializer", None)
                ):
                    specs.append((name, spec))
                    seen.add(name)

        descriptor_name = _ENTITY_DESCRIPTOR_TYPE_NAMES.get(kind)
        descriptor = (
            self.registry.types.by_name.get(descriptor_name)
            if descriptor_name is not None
            else None
        )
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
        owner_provider: Callable[[], object | None],
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
                raise RuntimeError(
                    f"materialized field {field_name!r} owner not initialised"
                )
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
        mapping_fn = getattr(value, "as_mapping", None)
        if not callable(mapping_fn):
            return value

        fields = dict(mapping_fn())
        changed = False
        descriptor_name = _ENTITY_DESCRIPTOR_TYPE_NAMES.get(kind)
        if descriptor_name is not None:
            descriptor = self.registry.types.by_name.get(descriptor_name)
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
            existing_child = fields.get(field_name, _MISSING)
            if existing_child is not _MISSING:
                existing_location = getattr(existing_child, "location", None)
                if getattr(existing_location, "type", None) != "virtual":
                    continue
            # Refresh evaluator-owned virtual children when an entity is
            # re-decorated after mutation. Existing virtual children may still
            # capture the old owner snapshot and would otherwise materialize
            # stale derived state such as raw JSON or lineage views.
            fields[field_name] = self._make_materialized_child_value(
                field_name=field_name,
                field_spec=field_spec,
                owner_provider=lambda: owner_snapshot,
                label_prefix=owner_name,
            )
            changed = True

        if not changed:
            return _wrap_registered_value(kind, value)
        owner_snapshot = _wrap_registered_value(kind, Struct(**fields))
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
        source_range_type = self.registry.types.by_name.get("mlody-source-range")
        if source_range_type is not None:
            fields["_entity_type"] = source_range_type
        return Struct(**fields)

    def _refresh_declared_entity_types(self) -> None:
        registry_buckets = (
            (self.registry.roots, "root"),
            (self.registry.values, "value"),
            (self.registry.tasks, "task"),
            (self.registry.actions, "action"),
        )
        for bucket, kind in registry_buckets:
            for mapping in (bucket.by_key, bucket.by_name):
                for key, value in list(mapping.items()):
                    mapping[key] = self.decorate_registered_value(kind, value)

        for key, value in list(self.registry.all.items()):
            if not (isinstance(key, tuple) and len(key) == 3):
                continue
            registered_kind = key[0]
            if (
                isinstance(registered_kind, str)
                and registered_kind in _ENTITY_DESCRIPTOR_TYPE_NAMES
            ):
                self.registry.all[key] = self.decorate_registered_value(registered_kind, value)

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

        self.registry.register(kind, key, thing)
        if kind == "type":
            self._refresh_declared_entity_types()
        _log.debug("Registered %r as %s", key, kind)

    def _lookup(self, kind: str, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        # Strip leading ':' so local-reference syntax (":foo") resolves like "foo".
        if name.startswith(":"):
            name = name[1:]
        registry = self.registry.for_kind(kind, operation="lookup")
        if name not in registry.by_name:
            raise NameError(f"No {kind} {name!r}. Available: {sorted(registry.by_name)}")
        return registry.by_name[name]

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
            root_registry = self.registry.roots.by_name
            if root_name not in root_registry:
                raise NameError(
                    f"load() references unknown root @{root_name!r}; "
                    f"available: {sorted(root_registry)}"
                )
            root_obj = root_registry[root_name]
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
            # Also spread _persistent_injections so that names registered by init
            # files (e.g. `mm` from mm.mlody) are visible in every subsequent file
            # without an explicit load().
            sandbox_globals: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
                "__builtins__": {**SAFE_BUILTINS, "print": self._print_fn},
                "__MLODY__": True,
                **self._persistent_injections,
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

            def _register_method(generic_name: str, method: Any) -> None:  # pyright: ignore[reportExplicitAny]
                entry = self._method_registry.setdefault(
                    generic_name, {"arity": None, "methods": []}
                )
                method_patterns: list[Any] = list(getattr(method, "patterns", []))  # pyright: ignore[reportExplicitAny]
                new_arity = len(method_patterns)
                existing_arity: int | None = entry["arity"]  # type: ignore[assignment]
                if existing_arity is not None and existing_arity != new_arity:
                    raise ValueError(
                        f"generic {generic_name!r} has arity {existing_arity}; "
                        f"cannot attach method with {new_arity} patterns"
                    )
                if existing_arity is None:
                    entry["arity"] = new_arity
                entry["methods"].append(method)  # type: ignore[union-attr]

            def _get_methods(generic_name: str) -> list[Any]:  # pyright: ignore[reportExplicitAny]
                entry = self._method_registry.get(generic_name)
                if entry is None:
                    return []
                return list(entry["methods"])  # type: ignore[index]

            def _dispatch_method(
                name: str,
                args: Any,
                methods: list[Any],  # pyright: ignore[reportExplicitAny]
            ) -> Any:  # pyright: ignore[reportExplicitAny]
                # Lazy import so that targets without a dep on mlody.core.multimethod
                # can still use the evaluator (e.g. rule_test, which has no mm dispatch).
                from mlody.core.multimethod import dispatch as _md

                return _md(name, tuple(args), methods)

            builtins_obj = Builtins(
                register=_register_for_file,
                lookup=self._lookup,
                ctx=ctx_struct,
                inject=_inject_into_sandbox,
                register_method=_register_method,
                get_methods=_get_methods,
                dispatch_method=_dispatch_method,
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
        (e.g., `self.registry.roots.by_key`).

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

        def _resolve_mapping_value(v: object) -> object:
            if not isinstance(v, str):
                return v
            try:
                return self._lookup("value", v)
            except NameError:
                # Legacy dict/Struct-based config values may store arbitrary
                # string literals rather than value labels.
                return v

        def _resolve_action(v: object) -> Named:
            if isinstance(v, str):
                return self._lookup("action", v)
            if hasattr(v, "as_mapping"):
                action_fields = dict(v.as_mapping())  # type: ignore[union-attr]
                action_fields["inputs"] = _resolve_port_collection(
                    action_fields.get("inputs", [])
                )
                action_fields["outputs"] = _resolve_port_collection(
                    action_fields.get("outputs", [])
                )
                action_fields["config"] = _resolve_port_collection(
                    action_fields.get("config", [])
                )
                return self.decorate_registered_value("action", Struct(**action_fields))
            return v  # type: ignore[return-value]

        def _resolve_port_collection(values: object) -> object:
            if isinstance(values, dict):
                return {
                    str(name): _resolve_mapping_value(value)
                    for name, value in values.items()
                }
            if isinstance(values, Struct):
                return Struct(
                    **{
                        str(name): _resolve_mapping_value(value)
                        for name, value in values.as_mapping().items()
                    }
                )
            if isinstance(values, (list, tuple)):
                return [_resolve_value(value) for value in values]
            return values

        actions = self.registry.actions
        # Resolve actions: string labels in inputs/outputs/config → value structs
        for key, entity in list(actions.by_key.items()):
            fields = dict(entity.as_mapping())
            fields["inputs"] = _resolve_port_collection(fields.get("inputs", []))
            fields["outputs"] = _resolve_port_collection(fields.get("outputs", []))
            fields["config"] = _resolve_port_collection(fields.get("config", []))
            new_entity = self.decorate_registered_value("action", Struct(**fields))
            self.registry.register("action", key, new_entity, replace=True)

        tasks = self.registry.tasks
        # Resolve tasks: string labels in inputs/outputs/config/action → entity structs
        for key, entity in list(tasks.by_key.items()):
            fields = dict(entity.as_mapping())
            fields["inputs"] = _resolve_port_collection(fields.get("inputs", []))
            fields["outputs"] = _resolve_port_collection(fields.get("outputs", []))
            fields["config"] = _resolve_port_collection(fields.get("config", []))
            fields["action"] = _resolve_action(fields.get("action"))
            new_entity = self.decorate_registered_value("task", Struct(**fields))
            self.registry.register("task", key, new_entity, replace=True)
