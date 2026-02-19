# Technical Specification: mlody — MVP

**Version:** 1.0
**Date:** 2026-02-16
**Prepared by:** Solution Architect AI
**Status:** Draft
**Source:** `mlody/REQUIREMENTS.md` v1.1

---

## 1. Executive Summary

mlody is a declarative ML pipeline framework that uses Starlark (via the existing `starlarkish` evaluator) to define pipelines in `.mlody` files. The MVP delivers three surface areas — a CLI tool, an LSP server, and a core library — enabling pipeline definition, inspection, and authoring support without execution.

**Key benefits:**
- Pipelines become queryable data structures, not imperative scripts
- Bazel-style target addressing provides familiar, composable references
- LSP integration brings IDE-grade authoring to `.mlody` files
- The architecture is layered and extensible: new CLI commands, LSP capabilities, and DSL primitives slot in with minimal wiring

**How it addresses requirements:**
- FR-001–FR-004 (CLI + targets): `mlody show` and `mlody shell` resolve Bazel-style target addresses against evaluated `.mlody` graphs
- FR-005–FR-007 (LSP): pygls server provides completion and go-to-definition for `.mlody` files
- FR-008–FR-012 (core): Workspace class wraps the starlarkish evaluator with two-phase loading and target resolution
- FR-010 (plan model): Stub dataclasses for future execution planning

---

## 2. Architecture Overview

### 2.1 Component Diagram

```
┌──────────────────────────────────────────────────┐
│                    CLI (click)                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │  main.py │  │ show.py  │  │   shell.py     │  │
│  │ (entry)  │  │ (cmd)    │  │ (ptpython REPL)│  │
│  └────┬─────┘  └────┬─────┘  └───────┬────────┘  │
│       │              │                │            │
├───────┼──────────────┼────────────────┼────────────┤
│       │         Core Library          │            │
│  ┌────┴──────────────┴────────────────┴─────────┐ │
│  │              workspace.py                     │ │
│  │  (two-phase loading, target resolution)       │ │
│  └───────────────────┬───────────────────────────┘ │
│                      │                              │
│  ┌───────────────────┴───┐  ┌────────────────────┐ │
│  │     targets.py        │  │     plan.py        │ │
│  │ (address parsing)     │  │ (execution stubs)  │ │
│  └───────────────────────┘  └────────────────────┘ │
├─────────────────────────────────────────────────────┤
│                  LSP Server (pygls)                  │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ server.py│  │completion.py │  │definition.py │  │
│  └────┬─────┘  └──────┬───────┘  └──────┬───────┘  │
│       └───────────────┼──────────────────┘          │
│                       │                              │
├───────────────────────┼──────────────────────────────┤
│            starlarkish evaluator                     │
│  ┌────────────────────┴──────────────────────────┐  │
│  │  Evaluator  │  Struct/struct  │  SAFE_BUILTINS │  │
│  └───────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### 2.2 Data Flow

1. **CLI invocation** → `main.py` verifies monorepo root, creates `Workspace`
2. **Two-phase loading** → Workspace evaluates `roots.mlody` (phase 1), then discovers and evaluates all `.mlody` files under each root (phase 2)
3. **Target resolution** → `targets.py` parses `@ROOT//path:target.field` syntax; `workspace.py` traverses the evaluator's `roots` dict and Struct fields
4. **Output** → `show.py` renders values via rich; `shell.py` exposes `show()` in a ptpython REPL
5. **LSP** → `server.py` initializes workspace on `initialize`; completion and definition handlers read evaluator state (`_module_globals`, `loaded_files`, `SAFE_BUILTINS`)

---

## 3. Technical Stack

| Category | Choice | Notes |
|----------|--------|-------|
| Language | Python 3.13.2 | Hermetic via rules_python |
| Build system | Bazel (Bazelisk) | `o_py_library`, `o_py_binary`, `o_py_test` from `//build/bzl:python.bzl` |
| CLI framework | click | Repo convention |
| Terminal output | rich | Repo convention |
| Interactive REPL | ptpython | **Needs addition to pyproject.toml** |
| LSP framework | pygls + lsprotocol | **Needs addition to pyproject.toml** |
| Evaluation engine | starlarkish | `//common/python/starlarkish` (internal) |
| Type checking | basedpyright strict | Repo convention |
| Formatting | ruff | Repo convention |
| Testing | pytest (auto-injected by `o_py_test`) | pyfakefs for filesystem mocking |

### 3.1 Dependency Additions

Add to `pyproject.toml` `dependencies` list:

```toml
  # LSP server framework for language servers
  # See: https://github.com/openlawlibrary/pygls
  "pygls",

  # Enhanced Python REPL with autocompletion
  # See: https://github.com/prompt-toolkit/ptpython
  "ptpython",
```

Then run `o-repin` to regenerate lock files.

---

## 4. Detailed Component Specifications

### 4.1 `mlody/core/targets.py` — Target Address Parsing

**Purpose:** Parse Bazel-style target addresses with `@ROOT` multi-root support and dot-access field traversal. Pure data module with no evaluator dependency.

**Traceability:** FR-001

#### Data Types

```python
@dataclass(frozen=True)
class TargetAddress:
    root: str | None            # None = default root; "TEAM_A" for @TEAM_A
    package_path: str | None    # "path/to" from //path/to:target; None for ":target"
    target_name: str            # "target" from :target
    field_path: tuple[str, ...]  # ("field", "sub") from .field.sub; empty if none
```

#### Functions

```python
def parse_target(raw: str) -> TargetAddress:
    """
    Parse target string into TargetAddress.

    Formats:
      @ROOT//path/to:target.field.subfield
      //path/to:target
      :target
      :target.field

    Raises ValueError on malformed input.
    """

def resolve_target_value(
    address: TargetAddress,
    roots: dict[str, Any],
) -> object:
    """
    Traverse roots dict -> target -> dot fields.

    Raises KeyError (missing root/target) or AttributeError (missing field)
    with descriptive messages.
    """
```

#### Parsing Algorithm

1. If starts with `@`: extract root name (everything between `@` and `//`); strip `@ROOT` prefix
2. If contains `//`: extract package_path (between `//` and `:`); remainder after `:`
3. Split remainder on first `.` → target_name + field_path components
4. Validate: target_name must be non-empty

#### Resolution Algorithm

1. Look up root in `roots` dict (use `None` key or first root for default)
2. Navigate package_path segments as Struct field traversal or dict lookup
3. Look up target_name
4. Traverse each field_path component via `getattr()` on Struct objects

**Dependencies:** None (stdlib only + `typing`)

---

### 4.2 `mlody/core/workspace.py` — Workspace & Evaluator Bridge

**Purpose:** Wraps the starlarkish `Evaluator` with two-phase loading, root management, and target resolution.

**Traceability:** FR-008, FR-009, FR-011, FR-012

#### Data Types

```python
@dataclass(frozen=True)
class RootInfo:
    name: str           # Root identifier, used as @NAME in targets
    path: str           # Root-relative path, e.g. "//mlody/teams/lexica"
    description: str    # Human-readable description
```

#### Class: Workspace

```python
class Workspace:
    def __init__(
        self,
        monorepo_root: Path,
        roots_file: Path | None = None,
    ) -> None:
        """
        Args:
            monorepo_root: Absolute path to monorepo root (must contain MODULE.bazel).
            roots_file: Path to roots.mlody. Defaults to monorepo_root / "mlody/roots.mlody".
        """

    @property
    def root_infos(self) -> dict[str, RootInfo]: ...

    @property
    def evaluator(self) -> Evaluator: ...

    def load(self) -> None:
        """Execute two-phase loading."""

    def resolve(self, target: str | TargetAddress) -> object:
        """Parse (if string) and resolve a target to a value."""
```

#### Two-Phase Loading

**Phase 1 — Root discovery:**
1. Create `Evaluator(root=monorepo_root)`
2. Call `evaluator.eval_file(roots_file)`
3. Read `evaluator.roots` → build `RootInfo` for each entry
4. Each root entry is a Struct with `name`, `path`, `description` fields (registered via `builtins.register("root", struct(...))`)

**Phase 2 — Full evaluation:**
1. For each `RootInfo`, convert `path` to absolute: `monorepo_root / path.lstrip("/")`
2. Glob `**/*.mlody` under that directory
3. Skip already-loaded files (`evaluator.loaded_files`)
4. Call `evaluator.eval_file()` on each discovered file

**Dependencies:**
- `common.python.starlarkish.evaluator.evaluator.Evaluator`
- `common.python.starlarkish.core.struct.Struct`
- `mlody.core.targets.parse_target`, `resolve_target_value`

---

### 4.3 `mlody/core/builtins.mlody` — Starlark Root Helper

**Purpose:** Provides the `root()` function used in `roots.mlody` to register named roots.

```starlark
def root(name, path, description=""):
    """Register a named root for target addressing."""
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
```

**Usage in `roots.mlody`:**
```starlark
load("//mlody/core/builtins.mlody", "root")

root(name="lexica", path="//mlody/teams/lexica", description="text ML team")
root(name="common", path="//mlody/common", description="shared modules")
```

---

### 4.4 `mlody/cli/main.py` — CLI Entry Point

**Purpose:** Click-based CLI with global options, monorepo root verification, and subcommand registration.

**Traceability:** FR-002, FR-014

```python
def verify_monorepo_root() -> Path:
    """
    Verify CWD contains MODULE.bazel.
    Returns monorepo root Path or calls sys.exit(1) with error message:
      "Error: mlody must be run from the monorepo root
       (expected MODULE.bazel in current directory).
       Please cd to the repo root and try again."
    """

@click.group()
@click.option("--roots", type=click.Path(exists=True), default=None,
              help="Path to roots.mlody (default: mlody/roots.mlody)")
@click.option("--verbose", is_flag=True, default=False,
              help="Increase output verbosity")
@click.pass_context
def cli(ctx: click.Context, roots: str | None, verbose: bool) -> None:
    """mlody — ML pipeline framework CLI."""

def main() -> None:
    """Entry point. Imports subcommands, invokes cli group."""
```

**Subcommand registration pattern:** Each subcommand module imports `cli` and decorates its function with `@cli.command()`. The `main()` function imports subcommand modules before calling `cli()`.

**Dependencies:** `click`, `mlody.core.workspace`

---

### 4.5 `mlody/cli/show.py` — Show Subcommand

**Purpose:** Resolve and display values from pipeline definitions.

**Traceability:** FR-001, FR-003

```python
@cli.command()
@click.argument("targets", nargs=-1, required=True)
@click.pass_context
def show(ctx: click.Context, targets: tuple[str, ...]) -> None:
    """Resolve and display pipeline values.

    TARGETS: One or more Bazel-style target references.
    """

def show_fn(workspace: Workspace, *targets: str) -> object | list[object]:
    """
    Functional form for shell REPL.
    Returns resolved values instead of printing.
    """
```

**Output format:** Rich-formatted using `rich.pretty.Pretty` for Struct/dict values, plain `str` for primitives. Errors rendered with `rich.console.Console.print` in red with suggestions of available targets.

**Dependencies:** `click`, `rich`, `mlody.core.workspace`, `mlody.core.targets`

---

### 4.6 `mlody/cli/shell.py` — Shell Subcommand

**Purpose:** Launch an interactive ptpython REPL with mlody commands available as functions.

**Traceability:** FR-004

```python
@cli.command()
@click.pass_context
def shell(ctx: click.Context) -> None:
    """Launch interactive mlody shell."""

def _build_repl_namespace(workspace: Workspace) -> dict[str, object]:
    """
    Build REPL namespace:
      show(*targets) -> resolve and return values
      workspace      -> Workspace instance for direct access
    """

def _launch_repl(namespace: dict[str, object]) -> None:
    """Launch ptpython with the given namespace."""
```

**Extensibility:** Adding a new CLI subcommand to the shell requires:
1. Add an `_fn` variant in the subcommand module
2. Add it to `_build_repl_namespace`

**Dependencies:** `click`, `ptpython`, `mlody.core.workspace`, `mlody.cli.show`

---

### 4.7 `mlody/lsp/server.py` — LSP Server

**Purpose:** pygls-based Language Server Protocol server for `.mlody` files.

**Traceability:** FR-005

```python
class MlodyLanguageServer(LanguageServer):
    workspace_: Workspace | None = None

    def initialize_workspace(self, root_path: Path) -> None:
        """Create and load Workspace from LSP root."""

server = MlodyLanguageServer()

@server.feature(types.INITIALIZE)
def on_initialize(params: types.InitializeParams) -> None: ...

@server.feature(types.TEXT_DOCUMENT_COMPLETION)
def on_completion(params: types.CompletionParams) -> types.CompletionList: ...

@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def on_definition(params: types.DefinitionParams) -> types.Location | None: ...

def main() -> None:
    """Start LSP server on stdio."""
```

**Transport:** stdio (standard for editor integration)
**File association:** `.mlody`

**Dependencies:** `pygls`, `lsprotocol`, `mlody.core.workspace`

---

### 4.8 `mlody/lsp/completion.py` — Completion Provider

**Purpose:** Context-aware completions in `.mlody` files.

**Traceability:** FR-006

```python
def provide_completions(
    server: MlodyLanguageServer,
    params: types.CompletionParams,
) -> types.CompletionList: ...
```

**Completion sources (by context):**

| Context | Source | Data origin |
|---------|--------|-------------|
| General typing | Safe builtins | `SAFE_BUILTINS` keys from evaluator |
| After `builtins.` | `register`, `ctx` | Hardcoded (from `Builtins` class) |
| Any position | Loaded symbols | `evaluator._module_globals[current_file]` |
| Inside `load("...")` | File paths | Filesystem glob under `//` or `:` relative dirs |

**Context detection:** Read current line text. If cursor is inside a `load("..."` string literal, provide path completions. If preceded by `builtins.`, provide member completions. Otherwise, provide builtins + loaded symbols.

**Dependencies:** `pygls`, `lsprotocol`, `mlody.lsp.server`, starlarkish `SAFE_BUILTINS`

---

### 4.9 `mlody/lsp/definition.py` — Go-to-Definition Provider

**Purpose:** Navigate to definitions of symbols and files from `.mlody` files.

**Traceability:** FR-007

```python
def provide_definition(
    server: MlodyLanguageServer,
    params: types.DefinitionParams,
) -> types.Location | list[types.Location] | None: ...
```

**Definition targets:**

| Cursor on | Resolution |
|-----------|------------|
| `load("//path/to/file.mlody", ...)` — the path string | Resolve path using evaluator conventions (`//` → root-absolute, `:` → sibling) → `Location(uri, Position(0, 0))` |
| Imported symbol name (e.g., `MY_VAR` from `load(...)`) | Find source file via `_module_globals`, search for `MY_VAR = ` or `def MY_VAR(` at file scope → `Location(uri, line)` |

**Dependencies:** `pygls`, `lsprotocol`, `mlody.lsp.server`

---

### 4.10 `mlody/core/plan.py` — Execution Plan Model (Stubs)

**Purpose:** Data structures for future execution planning. Stubs only.

**Traceability:** FR-010

```python
@runtime_checkable
class Activity(Protocol):
    """Protocol for all plan activities."""
    kind: str
    def to_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class BuildImage:
    kind: str = "build_image"
    image_name: str = ""
    dockerfile: str = ""
    def to_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class Execute:
    kind: str = "execute"
    command: str = ""
    def to_dict(self) -> dict[str, object]: ...

@dataclass
class Plan:
    activities: list[Activity]
    def to_dict(self) -> list[dict[str, object]]: ...
    def to_json(self) -> str: ...
```

**Dependencies:** None (stdlib only)

---

## 5. Data Architecture

### 5.1 Starlark Evaluation Model

The starlarkish evaluator provides a sandboxed execution environment. Key data flow:

```
.mlody file → exec() in sandbox → builtins.register("root", struct(...))
                                 → evaluator.roots["name"] = struct(...)
```

**Sandbox contents per file:**
- `__builtins__`: `SAFE_BUILTINS` dict (deny-by-default)
- `load`: bound `functools.partial` of `Evaluator._load` with `current_file` and `caller_globals`
- `builtins`: `Builtins` instance with `register(kind, thing)` and `ctx.directory`

**Caching:** `Evaluator._module_globals: dict[Path, dict[str, Any]]` stores sandbox globals per file. Subsequent `load()` calls return cached results.

### 5.2 Target Addressing Schema

```
@ROOT//package/path:target_name.field.subfield
│     │              │           │
│     │              │           └─ field_path: tuple[str, ...]
│     │              └─ target_name: str
│     └─ package_path: str
└─ root: str | None
```

**Resolution chain:**
```
roots dict → root object → (package navigation) → target → field.subfield
```

### 5.3 Workspace State

```python
Workspace
├── monorepo_root: Path
├── roots_file: Path
├── evaluator: Evaluator
│   ├── roots: dict[str, Named]        # Registered root objects
│   ├── targets: dict[str, Struct]     # (unused in MVP)
│   ├── loaded_files: set[Path]        # All evaluated files
│   └── _module_globals: dict[Path, dict[str, Any]]  # Per-file sandbox state
└── root_infos: dict[str, RootInfo]    # Parsed root metadata
```

---

## 6. API Specifications

### 6.1 CLI Interface

```
mlody [OPTIONS] COMMAND [ARGS]

Options:
  --roots PATH    Path to roots.mlody (default: mlody/roots.mlody)
  --verbose       Increase output verbosity

Commands:
  show TARGET [TARGET...]   Resolve and display pipeline values
  shell                     Launch interactive mlody shell
```

**Exit codes:**
- `0`: Success
- `1`: Monorepo root check failed, target not found, evaluation error

### 6.2 LSP Capabilities

| Capability | Method | Trigger |
|------------|--------|---------|
| Completion | `textDocument/completion` | Typing in `.mlody` files |
| Go-to-definition | `textDocument/definition` | Ctrl+click / gd on `load()` paths or symbols |

### 6.3 Python API (for shell and programmatic use)

```python
from mlody.core.workspace import Workspace

ws = Workspace(Path("/path/to/monorepo"))
ws.load()

value = ws.resolve("@lexica//models:bert.config.learning_rate")
```

---

## 7. Implementation Plan

### Phase 0: Foundation

| Step | File | Description | Deps |
|------|------|-------------|------|
| 0.1 | `pyproject.toml` | Add pygls, ptpython; run `o-repin` | — |
| 0.2 | `mlody/CLAUDE.md` | Project-specific directives | — |
| 0.3 | `mlody/core/__init__.py` | Package marker | — |
| 0.4 | `mlody/core/targets.py` | Target address parsing | — |
| 0.5 | `mlody/core/targets_test.py` | Tests for target parsing | 0.4 |
| 0.6 | `mlody/core/builtins.mlody` | Starlark `root()` helper | — |
| 0.7 | `mlody/core/workspace.py` | Evaluator bridge, two-phase loading | 0.4, 0.6 |
| 0.8 | `mlody/core/workspace_test.py` | Tests for workspace | 0.7 |
| 0.9 | `mlody/cli/__init__.py` | Package marker | — |
| 0.10 | `mlody/cli/main.py` | Click entry point, monorepo root check | 0.7 |
| 0.11 | `mlody/cli/main_test.py` | Tests for CLI entry | 0.10 |
| 0.12 | `mlody/BUILD.bazel` | Top-level BUILD | — |
| 0.13 | `mlody/core/BUILD.bazel` | Core BUILD targets | 0.4–0.8 |
| 0.14 | `mlody/cli/BUILD.bazel` | CLI BUILD targets | 0.9–0.11 |

### Phase 1: Core CLI (after Phase 0)

| Step | File | Description | Deps |
|------|------|-------------|------|
| 1.1 | `mlody/cli/show.py` | Show subcommand + `show_fn` | 0.10 |
| 1.2 | `mlody/cli/show_test.py` | Tests | 1.1 |
| 1.3 | `mlody/cli/shell.py` | Shell subcommand (ptpython REPL) | 1.1 |
| 1.4 | `mlody/cli/shell_test.py` | Tests | 1.3 |

### Phase 2: LSP (parallel with Phase 1)

| Step | File | Description | Deps |
|------|------|-------------|------|
| 2.1 | `mlody/lsp/__init__.py` | Package marker | — |
| 2.2 | `mlody/lsp/server.py` | pygls server setup | 0.7 |
| 2.3 | `mlody/lsp/completion.py` | Completion provider | 2.2 |
| 2.4 | `mlody/lsp/completion_test.py` | Tests | 2.3 |
| 2.5 | `mlody/lsp/definition.py` | Go-to-definition provider | 2.2 |
| 2.6 | `mlody/lsp/definition_test.py` | Tests | 2.5 |
| 2.7 | `mlody/lsp/BUILD.bazel` | LSP BUILD targets | 2.1–2.6 |

### Phase 3: Plan Stubs (anytime)

| Step | File | Description | Deps |
|------|------|-------------|------|
| 3.1 | `mlody/core/plan.py` | Execution plan data model stubs | — |
| 3.2 | `mlody/core/plan_test.py` | Tests | 3.1 |

---

## 8. Testing Strategy

### 8.1 Unit Tests

| Module | Test file | Key scenarios |
|--------|-----------|---------------|
| `targets.py` | `targets_test.py` | Parse all syntax variants (`@ROOT//path:target.field`, `//path:target`, `:target`); field traversal on Structs; error cases (missing root, missing field, malformed syntax) |
| `workspace.py` | `workspace_test.py` | Phase 1 root discovery; phase 2 file discovery and evaluation; `resolve()` delegation; missing roots file error |
| `main.py` | `main_test.py` | `verify_monorepo_root` positive/negative; CLI invocation with `--roots` and `--verbose` |
| `show.py` | `show_test.py` | Resolve simple target; field access; multiple targets; error rendering; `show_fn` returns values |
| `shell.py` | `shell_test.py` | Namespace includes `show` and `workspace`; ptpython embed is called |
| `completion.py` | `completion_test.py` | Builtin completions; `builtins.*` completions; loaded symbol completions; load path completions |
| `definition.py` | `definition_test.py` | Go-to-definition on `load()` path; on imported symbol; returns None for non-navigable positions |
| `plan.py` | `plan_test.py` | Protocol satisfaction; serialization to dict/JSON |

### 8.2 Testing Approach

- **Filesystem mocking:** Use `pyfakefs` (`fs` fixture) or starlarkish `InMemoryFS` for tests that need `.mlody` files
- **Click testing:** Use `click.testing.CliRunner` for CLI invocation tests
- **LSP testing:** Unit test provider functions directly, passing mock `MlodyLanguageServer` instances
- **No mocking of starlarkish internals** — test through the public `Evaluator` API with real (in-memory) `.mlody` files

### 8.3 Test Execution

```sh
bazel test //mlody/...                        # All tests
bazel test //mlody/core:targets_test          # Single test
bazel build --config=lint //mlody/...         # Lint check
```

---

## 9. Non-Functional Requirements

### 9.1 Code Quality
- **NFR-001:** All code passes basedpyright strict mode
- **NFR-002:** All code formatted with ruff
- **NFR-003:** Test coverage for core logic (target resolution, workspace loading, plan model)
- **NFR-004:** Tests use `o_py_test` (pytest + debugpy auto-injected)

### 9.2 Extensibility
- **NFR-005:** New CLI subcommand = new file with `@cli.command()` + `_fn` variant → appears in CLI and shell
- **NFR-006:** New LSP capability = new handler in `server.py` + new provider module
- **NFR-007:** New plan activity = new frozen dataclass implementing `Activity` protocol

### 9.3 Performance
- **NFR-010:** Evaluated `.mlody` files cached via `Evaluator._module_globals` (already implemented)
- **NFR-011:** LSP responds to completion/definition within 200ms for typical files

---

## 10. Risks & Mitigation

| ID | Risk | Impact | Probability | Mitigation |
|----|------|--------|-------------|------------|
| R-001 | Evaluator has debug `print()` statements (lines 119, 193, 225) that will pollute CLI output | Medium | High | Suppress or remove debug prints before MVP ship; or redirect evaluator stdout during CLI use |
| R-002 | `evaluator._module_globals` is a private attribute accessed by LSP for completions | Low | High | Document as intentional coupling; consider adding a public accessor to starlarkish in a follow-up |
| R-003 | `evaluator.targets` dict is never populated (only `roots` is handled in `_register`) | Low | Low | MVP only uses `roots`; extend `_register` when target registration is needed |
| R-004 | pygls/ptpython may have version conflicts with existing deps | Medium | Low | Run `o-repin` early to surface conflicts before implementation begins |
| R-005 | Two-phase loading evaluates all `.mlody` files eagerly — could be slow for large root trees | Low | Low | Acceptable for MVP; graph pruning noted as future optimization (FR out of scope) |

---

## 11. Future Considerations

- **Pipeline execution runtime** — local and distributed executors consuming `Plan` objects
- **Advanced query language** — XPath/jq-style or SQL queries over the pipeline graph
- **Graph pruning** — lazy evaluation: only load files needed for a specific target query
- **LSP diagnostics** — type checking, unused imports, undefined symbols
- **LSP hover** — show type/value info on hover
- **Team grouping** — `team()` primitive grouping multiple roots with metadata (FR-013)
- **`__all__` support** — respect module-level `__all__` in `load()` (evaluator TODO at line 145)
- **UI/web dashboard** — visual pipeline inspector

---

## 12. Project Structure

```
mlody/
├── CLAUDE.md                  # Project-specific directives
├── REQUIREMENTS.md            # Requirements document (exists)
├── SPEC.md                    # This specification (exists)
├── BUILD.bazel                # Top-level BUILD
├── core/
│   ├── __init__.py
│   ├── builtins.mlody         # Starlark root() helper
│   ├── targets.py             # Target address parsing
│   ├── targets_test.py
│   ├── workspace.py           # Evaluator bridge, two-phase loading
│   ├── workspace_test.py
│   ├── plan.py                # Execution plan stubs
│   ├── plan_test.py
│   └── BUILD.bazel
├── cli/
│   ├── __init__.py
│   ├── main.py                # Click entry point
│   ├── main_test.py
│   ├── show.py                # show subcommand
│   ├── show_test.py
│   ├── shell.py               # shell subcommand (ptpython REPL)
│   ├── shell_test.py
│   └── BUILD.bazel
└── lsp/
    ├── __init__.py
    ├── server.py              # pygls LSP server
    ├── completion.py          # Completion provider
    ├── completion_test.py
    ├── definition.py          # Go-to-definition provider
    ├── definition_test.py
    └── BUILD.bazel
```

---

## 13. BUILD.bazel Specifications

### 13.1 `mlody/BUILD.bazel`

```starlark
# Top-level BUILD for mlody package
```

### 13.2 `mlody/core/BUILD.bazel`

```starlark
load("//build/bzl:python.bzl", "o_py_library", "o_py_test")

o_py_library(
    name = "targets_lib",
    srcs = ["targets.py"],
    visibility = ["//:__subpackages__"],
)

o_py_library(
    name = "workspace_lib",
    srcs = ["workspace.py"],
    data = ["builtins.mlody"],
    visibility = ["//:__subpackages__"],
    deps = [
        ":targets_lib",
        "//common/python/starlarkish/core:core_lib",
        "//common/python/starlarkish/evaluator:evaluator_lib",
    ],
)

o_py_library(
    name = "plan_lib",
    srcs = ["plan.py"],
    visibility = ["//:__subpackages__"],
)

o_py_test(
    name = "targets_test",
    srcs = ["targets_test.py"],
    deps = [
        ":targets_lib",
        "//common/python/starlarkish/core:core_lib",
    ],
)

o_py_test(
    name = "workspace_test",
    srcs = ["workspace_test.py"],
    deps = [
        ":workspace_lib",
        "@pip//pyfakefs",
    ],
)

o_py_test(
    name = "plan_test",
    srcs = ["plan_test.py"],
    deps = [":plan_lib"],
)
```

### 13.3 `mlody/cli/BUILD.bazel`

```starlark
load("//build/bzl:python.bzl", "o_py_binary", "o_py_library", "o_py_test")

o_py_library(
    name = "cli_lib",
    srcs = [
        "__init__.py",
        "main.py",
        "shell.py",
        "show.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "//mlody/core:targets_lib",
        "//mlody/core:workspace_lib",
        "@pip//click",
        "@pip//ptpython",
        "@pip//rich",
    ],
)

o_py_binary(
    name = "mlody",
    srcs = ["main.py"],
    main = "main.py",
    visibility = ["//:__subpackages__"],
    deps = [":cli_lib"],
)

o_py_test(
    name = "main_test",
    srcs = ["main_test.py"],
    deps = [
        ":cli_lib",
        "@pip//pyfakefs",
    ],
)

o_py_test(
    name = "show_test",
    srcs = ["show_test.py"],
    deps = [
        ":cli_lib",
        "@pip//pyfakefs",
    ],
)

o_py_test(
    name = "shell_test",
    srcs = ["shell_test.py"],
    deps = [":cli_lib"],
)
```

### 13.4 `mlody/lsp/BUILD.bazel`

```starlark
load("//build/bzl:python.bzl", "o_py_binary", "o_py_library", "o_py_test")

o_py_library(
    name = "lsp_lib",
    srcs = [
        "__init__.py",
        "completion.py",
        "definition.py",
        "server.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "//common/python/starlarkish/evaluator:evaluator_lib",
        "//mlody/core:workspace_lib",
        "@pip//lsprotocol",
        "@pip//pygls",
    ],
)

o_py_binary(
    name = "mlody_lsp",
    srcs = ["server.py"],
    main = "server.py",
    visibility = ["//:__subpackages__"],
    deps = [":lsp_lib"],
)

o_py_test(
    name = "completion_test",
    srcs = ["completion_test.py"],
    deps = [
        ":lsp_lib",
        "@pip//pyfakefs",
    ],
)

o_py_test(
    name = "definition_test",
    srcs = ["definition_test.py"],
    deps = [
        ":lsp_lib",
        "@pip//pyfakefs",
    ],
)
```

---

## 14. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Target parsing is a separate pure module (`targets.py`) | Easy to test in isolation; no evaluator dependency |
| Each CLI subcommand exposes an `_fn` variant for shell use | Explicit bridging avoids Click internals leakage; easy to extend |
| `builtins.mlody` is a Starlark file, not Python | Keeps the pattern consistent — everything is Starlark-first; `roots.mlody` uses `load()` to get helpers |
| LSP reuses evaluator's cached state (no re-evaluation per request) | Acceptable for MVP; document changes would ideally trigger re-evaluation (future) |
| `Workspace` exposes `evaluator` property for LSP access | Pragmatic — LSP needs `_module_globals` and `loaded_files`; refactoring evaluator is out of scope |
| Plan generation is separate from plan rendering | Enables future format options (JSON, YAML, terminal) without touching core logic |

---

## Appendix A: Glossary

- **`.mlody` file** — A Starlark script defining pipeline configuration, evaluated in a sandbox
- **Root** — A named entry point in the pipeline forest, registered via `builtins.register("root", ...)`
- **Target address** — Bazel-style reference: `@ROOT//path:target.field`
- **Two-phase loading** — Phase 1: evaluate `roots.mlody` for root discovery; Phase 2: evaluate all `.mlody` files under each root
- **Struct** — Immutable dot-accessible data type from `starlarkish`, analogous to Starlark's `struct()`
- **Sandbox** — Restricted execution environment with `SAFE_BUILTINS` only (deny-by-default)

## Appendix B: Existing Code References

| Component | Path | Key exports |
|-----------|------|-------------|
| Evaluator | `common/python/starlarkish/evaluator/evaluator.py` | `Evaluator`, `SAFE_BUILTINS`, `Builtins`, `Named` |
| Struct | `common/python/starlarkish/core/struct.py` | `Struct`, `struct()` |
| InMemoryFS | `common/python/starlarkish/evaluator/testing.py` | `InMemoryFS` |
| Bazel rules | `build/bzl/python.bzl` | `o_py_library`, `o_py_binary`, `o_py_test` |
| Dependencies | `pyproject.toml` | Root dependency list |

---

**End of Technical Specification**
