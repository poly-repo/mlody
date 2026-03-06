# mlody

Declarative ML pipeline framework. Pipelines are defined in `.mlody` files
(Starlark syntax), evaluated at runtime by the `starlarkish` evaluator
(`//common/python/starlarkish`). The Python code is the host; `.mlody` files are
the user-facing DSL.

## Components

- **`mlody/core/`** — evaluator bridge, target addressing, execution plan stubs
- **`mlody/cli/`** — click-based CLI (`mlody show`, `mlody shell`)
- **`mlody/lsp/`** — pygls LSP server for `.mlody` files (see `lsp/CLAUDE.md`
  for detailed library quirks)
- **`mlody/common/`** — shared Python utilities

## `.mlody` Files

Files ending in `.mlody` are **Starlark**, not Python. The goal is eventual pure
Starlark; Python-only features are acceptable only during prototyping and must
be marked explicitly.

### Differences between python and starlark

- in starlark, there's no 'is None' and 'is not None'; use '==' and '!='

### Sandbox (what is available inside `.mlody`)

| Symbol            | Description                                                                                                                                                                 |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `struct(...)`     | Immutable dot-accessible data type                                                                                                                                          |
| `Struct`          | The `Struct` class itself                                                                                                                                                   |
| `load(path, ...)` | Import symbols from other `.mlody` files                                                                                                                                    |
| `builtins`        | Host communication object (see below)                                                                                                                                       |
| `python.*`        | Python-specific escapes (audit target, avoid)                                                                                                                               |
| Standard builtins | `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `print`, `range`, `repr`, `reversed`, `set`, `sorted`, `str`, `tuple`, `zip` |

`type`, file I/O, imports, and all other builtins are **not available**.

### Communicating back to Python

```starlark
# Register a named object — the only way to export data to the host
builtins.register("root", struct(name="lexica", path="//mlody/teams/lexica"))

# Access the current file's directory
dir = builtins.ctx.directory
```

Only `"root"` is a supported kind in the current evaluator. Extend
`Evaluator._register` in `evaluator.py` to add new kinds.

### `python.*` namespace

Python features not valid in Starlark live under `python.`:

```starlark
python.hasattr(obj, "field")
python.getattr(obj, "field")
python.round(x, 2)
python.sum(values)
```

Every use of `python.` is an audit marker — run `grep python\.` to find all
places that need attention before migrating to pure Starlark. **Always ask
before adding new `python.*` features.**

### `load()` paths

```starlark
load("@lexica//models:bert.mlody", "BERT")    # root-anchored: @ROOT//package:file
load("@lexica//:helpers.mlody", "helper_fn")  # root-anchored, no package
load("//mlody/core/builtins.mlody", "root")   # repo-root-absolute
load(":helpers.mlody", "helper_fn")           # sibling file
load("subdir/file.mlody", "sym")              # relative to current file
```

For `@ROOT//` paths the named root must already be registered (via
`builtins.register("root", ...)`) before the `load()` is reached. This is always
true during Phase 2 workspace loading (roots are registered in Phase 1). Loading
the same resolved file more than once is safe — the evaluator returns cached
globals on subsequent calls, so Phase 2's glob re-discovering a file already
loaded via `@ROOT//` is a no-op.

## Target Addressing

```
@ROOT//package/path:target_name.field.subfield
```

- `@ROOT` — optional root name (registered via `builtins.register("root", ...)`)
- `//package/path` — optional package path
- `:target_name` — required target name
- `.field.subfield` — optional dot-access field traversal on `Struct` objects

Parsing and resolution live in `mlody/core/targets.py`.

## Workspace & Two-Phase Loading

`Workspace` (`mlody/core/workspace.py`) wraps the evaluator:

1. **Phase 1** — evaluates `mlody/roots.mlody` to discover named roots
2. **Phase 2** — globs `**/*.mlody` under each root directory and evaluates all
   files (skipping already-loaded ones)

```python
from mlody.core.workspace import Workspace

ws = Workspace(monorepo_root)
ws.load()
value = ws.resolve("@lexica//models:bert.config.learning_rate")
```

The `Workspace.evaluator` property exposes the underlying `Evaluator` instance
(needed by LSP for `_module_globals` and `loaded_files`).

## Starlarkish Internals (read-only reference)

| Symbol                       | Location                                           |
| ---------------------------- | -------------------------------------------------- |
| `Evaluator`, `SAFE_BUILTINS` | `common/python/starlarkish/evaluator/evaluator.py` |
| `Struct`, `struct()`         | `common/python/starlarkish/core/struct.py`         |
| `InMemoryFS` (test helper)   | `common/python/starlarkish/evaluator/testing.py`   |

Do **not** modify starlarkish internals from within mlody.

## Testing

- **Filesystem:** Use `pyfakefs` (`fs` fixture) or starlarkish `InMemoryFS` to
  create in-memory `.mlody` files — never touch the real filesystem in tests.
- **CLI:** Use `click.testing.CliRunner`.
- **LSP:** Unit-test provider functions directly with a mock
  `MlodyLanguageServer`; do not spin up a real server.
- **No mocking of starlarkish** — test through the public `Evaluator` API with
  real (in-memory) `.mlody` content.

```sh
bazel test //mlody/...                     # all mlody tests
bazel test //mlody/core:targets_test       # single test target
bazel build --config=lint //mlody/...     # lint
```

## BUILD Rules

Use `o_py_library`, `o_py_binary`, `o_py_test` from `//build/bzl:python.bzl`
(never raw `py_*`). `o_py_test` auto-injects pytest and debugpy.

Key `@pip` deps: `@pip//click`, `@pip//rich`, `@pip//ptpython`, `@pip//pygls`,
`@pip//lsprotocol`, `@pip//pyfakefs` (test only).

`.mlody` data files must be listed in `data = [...]` of any `o_py_library` that
loads them (e.g. `data = ["builtins.mlody"]`).
