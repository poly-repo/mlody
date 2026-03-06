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
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from common.python.starlarkish.core.struct import struct, Struct

_log = logging.getLogger(__name__)


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
    if isinstance(obj, bool):   # bool precedes int — bool is a subclass of int
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


# Python-specific builtins that are not part of the Starlark standard.
# These will be exposed under a `python` object.
PYTHON_SPECIFIC_BUILTINS = struct(
    hasattr=builtins.hasattr,
    getattr=builtins.getattr,
    round=builtins.round,
    sum=builtins.sum,
    Any=Any,
    Callable=Callable,
    re=re,
)

# A curated list of safe built-ins to expose to user scripts.
# This aligns with the "deny-by-default" security policy.
# NOTE: Python's built-in `type` is NOT exposed here.  Instead, 'type' maps to
# `_sandbox_type`, a safe string-returning wrapper with Starlark semantics.
# `isinstance` is safe: scripts can only test against classes already in the sandbox.
# Exception classes (ValueError, TypeError, NotImplementedError) are safe to raise/catch.
SAFE_BUILTINS: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
    'abs': builtins.abs,
    'all': builtins.all,
    'any': builtins.any,
    'bool': builtins.bool,
    'dict': builtins.dict,
    'enumerate': builtins.enumerate,
    'float': builtins.float,
    'int': builtins.int,
    'len': builtins.len,
    'list': builtins.list,
    'max': builtins.max,
    'min': builtins.min,
    'print': builtins.print,
    'range': builtins.range,
    'repr': builtins.repr,
    'reversed': builtins.reversed,
    'set': builtins.set,
    'sorted': builtins.sorted,
    'isinstance': builtins.isinstance,
    'str': builtins.str,
    'struct': struct,
    'tuple': builtins.tuple,
    'type': _sandbox_type,
    'ValueError': ValueError,
    'TypeError': TypeError,
    'NotImplementedError': NotImplementedError,
    'zip': builtins.zip,
    'None': None,
    'True': True,
    'False': False,
    'Struct': Struct,
    'python': PYTHON_SPECIFIC_BUILTINS,
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
    ) -> None:
        self.loaded_files: set[Path] = set()
        self._eval_stack: list[Path] = []
        self.root_path: Path = root
        self.roots: dict[str, Named] = dict()
        self.types: dict[str, Named] = dict()
        self.locations: dict[str, Named] = dict()
        self._roots_by_name: dict[str, Named] = {}
        self._types_by_name: dict[str, Named] = {}
        self._locations_by_name: dict[str, Named] = {}
        for _pname in ["integer", "string", "bool", "float"]:
            _sentinel: Named = Struct(  # type: ignore[assignment]
                kind="type", type=_pname, name=_pname, attributes={}, _allowed_attrs={}
            )
            self.types[_pname] = _sentinel          # bare key — no file ctx at init time
            self._types_by_name[_pname] = _sentinel
        self._module_globals: dict[Path, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        # Override print in the sandbox so callers can suppress stdout writes
        # (e.g. the LSP server, which uses stdout as its JSON-RPC transport).
        self._print_fn = print_fn
        # Extra fields merged into builtins.ctx for every file (e.g. workspace/run info).
        self._extra_ctx = extra_ctx
        if init_files:
            for init_file in init_files:
                path_to_load = init_file
                if not path_to_load.is_absolute():
                    path_to_load = self.root_path / path_to_load
                self._execute_file(path_to_load)

    def _register(self, kind: str, thing: Named, ctx: Struct) -> None:
        try:
            rel_dir = ctx.directory.relative_to(self.root_path)
        except ValueError:
            rel_dir = Path(".")
        key = str(rel_dir / thing.name)
        if kind == 'root':
            self.roots[key] = thing
            self._roots_by_name[thing.name] = thing
        elif kind == 'type':
            self.types[key] = thing
            self._types_by_name[thing.name] = thing
        elif kind == 'location':
            self.locations[key] = thing
            self._locations_by_name[thing.name] = thing
        else:
            raise ValueError(
                f"Unknown registration kind {kind!r}. Supported kinds: 'root', 'type', 'location'."
            )
        _log.debug("Registering %s as %s with key %r", thing, kind, key)

    def _lookup(self, kind: str, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        if kind == "type":
            if name not in self._types_by_name:
                raise NameError(f"No type {name!r}. Available: {sorted(self._types_by_name)}")
            return self._types_by_name[name]
        elif kind == "root":
            if name not in self._roots_by_name:
                raise NameError(f"No root {name!r}. Available: {sorted(self._roots_by_name)}")
            return self._roots_by_name[name]
        elif kind == "location":
            if name not in self._locations_by_name:
                raise NameError(f"No location {name!r}. Available: {sorted(self._locations_by_name)}")
            return self._locations_by_name[name]
        else:
            raise ValueError(f"Unknown lookup kind {kind!r}. Supported: 'root', 'type', 'location'.")

    def _load(self, path: str, *symbols: str, current_file: Path, caller_globals: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
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
            rest = path[slashslash + 2:]  # strip leading "//"
            if ":" not in rest:
                raise ValueError(
                    f"load() path {path!r} must contain ':' after '@ROOT//package'"
                )
            colon = rest.index(":")
            package = rest[:colon]
            filename = rest[colon + 1:]
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
                name for name in target_globals.keys()
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
            raise ImportError(f"Circular import detected: {' -> '.join(map(str, stack_copy))}")

        if file_path in self.loaded_files:
            # If already loaded, return the cached globals dict from the first execution.
            return self._module_globals.get(file_path, {})

        self._eval_stack.append(file_path)
        try:
            self.loaded_files.add(file_path)

            _log.debug("Evaluating %s", file_path)

            with open(file_path, 'r', encoding='utf-8') as f:
                script_content = f.read()

            _validate_loads_at_top(script_content, file_path)

            # Prepare sandbox globals.  Spread SAFE_BUILTINS and override "print"
            # with the instance-level print_fn so that callers (e.g. the LSP server)
            # can suppress sandbox stdout writes without mutating the shared
            # module-level constant.
            sandbox_globals: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
                "__builtins__": {**SAFE_BUILTINS, "print": self._print_fn},
                "__MLODY__": True,
            }

            ctx_kwargs: dict[str, Any] = {"directory": file_path.parent}  # pyright: ignore[reportExplicitAny]
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
                call_ctx = Struct(**{  # pyright: ignore[reportExplicitAny]
                    "directory": current_file.parent,
                    **(self._extra_ctx.as_mapping() if self._extra_ctx is not None else {}),
                })
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

            # create a load function that will inject into this sandbox's globals
            load_func = functools.partial(self._load, current_file=file_path, caller_globals=sandbox_globals)
            sandbox_globals["load"] = load_func

            # Register sandbox_globals BEFORE exec so that _inject_into_sandbox can
            # look up the current file's globals via self._module_globals during execution.
            self._module_globals[file_path] = sandbox_globals

            # Execute the file in its sandbox
            exec(script_content, sandbox_globals)

            _log.debug("Globals for %s: %s", file_path, list(sandbox_globals.keys()))

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
