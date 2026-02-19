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
  supports root-relative (``//path/to/file``) and file-relative (``:file``)
  paths.

- **Registration**: Scripts communicate results back to the host system via
  ``builtins.register(kind: str, thing: Struct)``.  The ``Evaluator`` instance
  collects registered objects in its internal state (e.g. ``self.roots``),
  accessible after evaluation completes.
"""
import builtins
import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from common.python.starlarkish.core.struct import struct, Struct

_log = logging.getLogger(__name__)


class Named(Protocol):
    """A protocol for objects with a 'name' attribute."""
    name: str


# Python-specific builtins that are not part of the Starlark standard.
# These will be exposed under a `python` object.
PYTHON_SPECIFIC_BUILTINS = struct(
    hasattr=builtins.hasattr,
    getattr=builtins.getattr,
    round=builtins.round,
    sum=builtins.sum,
    Any=Any,
    Callable=Callable,
)

# A curated list of safe built-ins to expose to user scripts.
# This aligns with the "deny-by-default" security policy.
# NOTE: `type` is intentionally excluded — it is a well-known exec-sandbox
# escape vector in Python.
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
    'str': builtins.str,
    'struct': struct,
    'tuple': builtins.tuple,
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
    ctx: Struct


class Evaluator:
    """
    The core engine that prepares a sandboxed environment and executes a user script.
    """
    def __init__(
        self,
        root: Path,
        init_files: list[Path] | None = None,
        print_fn: Callable[..., None] = builtins.print,
    ) -> None:
        self.loaded_files: set[Path] = set()
        self._eval_stack: list[Path] = []
        self.root_path: Path = root
        self.roots: dict[str, Named] = dict()
        self._module_globals: dict[Path, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        # Override print in the sandbox so callers can suppress stdout writes
        # (e.g. the LSP server, which uses stdout as its JSON-RPC transport).
        self._print_fn = print_fn
        if init_files:
            for init_file in init_files:
                path_to_load = init_file
                if not path_to_load.is_absolute():
                    path_to_load = self.root_path / path_to_load
                self._execute_file(path_to_load)

    def _register(self, kind: str, thing: Named) -> None:
        if kind == 'root':
            self.roots[thing.name] = thing
        else:
            raise ValueError(
                f"Unknown registration kind {kind!r}. Supported kinds: 'root'."
            )
        _log.debug("Registering %s as %s", thing, kind)

    def _load(self, path: str, *symbols: str, current_file: Path, caller_globals: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """
        Implementation of the Starlark-like `load()`:
         - path: file path (relative or //-absolute)
         - current_file: Path of the file that invoked load()
         - caller_globals: the globals dict of the caller (so we can inject symbols)
         - *symbols: optional names to import; if omitted, import all public globals
        """
        if path.startswith("//"):
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

            # Prepare sandbox globals.  Spread SAFE_BUILTINS and override "print"
            # with the instance-level print_fn so that callers (e.g. the LSP server)
            # can suppress sandbox stdout writes without mutating the shared
            # module-level constant.
            sandbox_globals: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
                "__builtins__": {**SAFE_BUILTINS, "print": self._print_fn},
                "__MLODY__": True,
            }

            builtins_obj = Builtins(
                register=self._register,
                ctx=struct(
                    directory=file_path.parent
                )
            )
            sandbox_globals["builtins"] = builtins_obj

            # create a load function that will inject into this sandbox's globals
            load_func = functools.partial(self._load, current_file=file_path, caller_globals=sandbox_globals)
            sandbox_globals["load"] = load_func

            # Execute the file in its sandbox
            exec(script_content, sandbox_globals)

            # Save module globals so subsequent loads return same dict (and avoid re-exec)
            self._module_globals[file_path] = sandbox_globals

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
