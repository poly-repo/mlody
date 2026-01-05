"""
Starlark-like Evaluator for .mlody files.

This module provides a sandboxed Python environment for executing user-defined
scripts with a `.mlody` extension. It is designed to safely evaluate
configuration or definition files in a controlled manner, similar to build
systems like Bazel that use Starlark.

Core Concepts:
- **Sandboxing**: Scripts are executed with a limited, explicitly-defined set of
  globally available functions and types, specified in the `SAFE_BUILTINS`
  dictionary. This prevents scripts from accessing arbitrary I/O or other
  unsafe operations.

- **Evaluator Class**: The main entry point is the `Evaluator` class. An instance
  of this class manages the state of the evaluation, including loaded files and
  registered objects.

- **`load()` Statement**: Scripts can import symbols from other `.mlody` files
  using a custom `load()` function, which is injected into the sandbox. It
  supports root-relative ("//path/to/file") and file-relative (":file") paths.

- **Registration**: Scripts can communicate results back to the host system by
  using the `builtins.register(kind: str, thing: Struct)` function. The
  `Evaluator` instance collects these registered objects (e.g., 'root' objects)
  in its internal state (`self.roots`, `self.targets`), which can be accessed
  after evaluation is complete.
"""
import builtins
import functools
from pathlib import Path
from typing import Any, Callable, Protocol

from common.python.starlarkish.core.struct import struct, Struct


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
    'type': builtins.type,
    'zip': builtins.zip,
    'None': None,
    'True': True,
    'False': False,
#    'Exception': Exception,
    'Struct': Struct,
    'python': PYTHON_SPECIFIC_BUILTINS,
}

class Builtins:
    register: Callable[[str, Named], None]
    ctx: Struct

    def __init__(self, register: Callable[[str, Named], None], ctx: Struct) -> None:
        self.register = register
        self.ctx = ctx


class Evaluator:
    """
    The core engine that prepares a sandboxed environment and executes a user script.
    """
    def __init__(self, root: Path, init_files: list[Path] | None = None) -> None:
        self.loaded_files: set[Path] = set()
        self._eval_stack: list[Path] = []
        #self.context = mlody_context.EvaluationContext()
        self.root_path: Path = root
        self.targets: dict[str, Struct] = dict()
        self.roots: dict[str, Named] = dict()
        self._module_globals: dict[Path, dict[str, Any]] = {}  # pyright: ignore[reportExplicitAny]
        if init_files:
            for init_file in init_files:
                path_to_load = init_file
                if not path_to_load.is_absolute():
                    path_to_load = self.root_path / path_to_load
                self._execute_file(path_to_load)


    def _register(self, kind : str, thing: Named) -> None:
        if kind == 'root':
            self.roots[thing.name] = thing
        print(f"REGISTERING {thing} as {kind}")
        
    def _load(self, path: str, *symbols: str, current_file: Path, caller_globals: dict[str, Any]) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
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

        if not target_globals:
            return {}

        # Decide which symbols to import
        # TODO: here maybe allow modules to define a symbol __all__, a list of everything imported
        # when no symbol is explicitly mentioned
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
            # if name in caller_globals:
            #     raise RuntimeError(f"Refusing to overwrite existing name {name!r} in caller")
            caller_globals[name] = target_globals[name]

            # if name in target_globals:
            #     # avoid overwriting caller's __builtins__ or load
            #     if name in ("__builtins__", "load"):
            #         continue
            #     caller_globals[name] = target_globals[name]

        # Optionally return the copied names or the entire module globals
        return target_globals


    def _execute_file(self, file_path: Path) -> dict[str, Any] :  # pyright: ignore[reportExplicitAny]
        """Executes a single .mlody file and returns the globals dict."""
        if file_path in self._eval_stack:
            stack_copy = self._eval_stack + [file_path]
            raise ImportError(f"Circular import detected: {' -> '.join(map(str, stack_copy))}")

        if file_path in self.loaded_files:
            # If already loaded, we should locate whichever globals were used the first time.
            # For simplicity, we can keep a map self._module_globals: Path -> globals dict.
            return self._module_globals.get(file_path, {})

        self._eval_stack.append(file_path)
        try:
            self.loaded_files.add(file_path)

            relative_path = file_path.relative_to(self.root_path)
            path_prefix = list(relative_path.parts[:-1]) + [relative_path.stem]
            print(f">>> {path_prefix} {self.targets}")
            
            with open(file_path, 'r') as f:
                script_content = f.read()

            # Prepare sandbox globals. Note: we create the dict first, then bind load into it
            sandbox_globals: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
                "__builtins__": SAFE_BUILTINS,
                "__MLODY__": True,
               # "targets": self.targets,
                # "load" will be set below after sandbox_globals exists
            }

            builtins = Builtins(
                register=self._register,
                ctx=struct(
                    directory=file_path.parent
                )
            )
            sandbox_globals["builtins"] = builtins
            
            # create a load function that will inject into this sandbox's globals
            load_func = functools.partial(self._load, current_file=file_path, caller_globals=sandbox_globals)
            sandbox_globals["load"] = load_func

            # Execute the file in its sandbox
            #print(f"===== {file_path} =====\n{script_content}\n=====\n")
            exec(script_content, sandbox_globals)

            # Save module globals so subsequent loads return same dict (and avoid re-exec)
            self._module_globals[file_path] = sandbox_globals

            print(f"GLOBALS for {file_path}: {list(sandbox_globals.keys())}")

            return sandbox_globals
        finally:
            self._eval_stack.pop()


    def eval_file(self, entrypoint_file: Path):
        """
        Evaluates a script and any scripts it loads.

        The results of the evaluation are stored in the evaluator's state
        (e.g., `self.roots`).

        Args:
            entrypoint_file: The path to the root script to execute.
        """
        # token = mlody_context.set_current_context(self.context)

        try:
            _ = self._execute_file(entrypoint_file)
        finally:
            pass
            # Always ensure the context is reset, even if errors occur.
#            mlody_context.reset_current_context(token)

 #       return self.context.pipeline
