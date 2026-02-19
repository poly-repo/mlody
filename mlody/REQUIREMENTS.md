# Requirements Document: mlody — MVP

**Version:** 1.0
**Date:** 2026-02-16
**Prepared by:** Requirements Analyst AI
**Status:** Draft

---

## 1. Executive Summary

**Problem:** ML training and evaluation pipelines are currently defined imperatively, making them difficult to automatically reason about, inspect, modify, and compose across teams.

**Solution:** mlody is a framework for defining pipelines declaratively using Starlark (via the `starlarkish` evaluator). Pipeline definitions are written in `.mlody` files using a familiar Python-like syntax, but once loaded, the resulting graph is frozen and purely declarative. This enables tooling to inspect, query, prune, and plan execution over pipeline definitions without running them.

**MVP Scope:** Skeleton implementations of a CLI tool (`mlody shell`, `mlody show`) and an LSP server (completion, go-to-definition), integrated with the existing `starlarkish` evaluator. No pipeline execution — only definition, inspection, and execution plan generation.

**Success Metrics:**
- Pipeline definitions can be loaded from `.mlody` files and queried via CLI
- `mlody show` resolves Bazel-style target references to values
- LSP provides completion and go-to-definition for `.mlody` files
- Architecture supports incremental addition of commands, query languages, and DSL primitives

---

## 2. Project Scope

### 2.1 In Scope

- **CLI tool** with `shell` and `show` subcommands
- **LSP server** with completion and go-to-definition
- **Starlarkish integration** — bidirectional value passing (Python→Starlark builtins, Starlark→Python via `register()`)
- **Target addressing** — Bazel-style target syntax with `@ROOT` multi-root support
- **Execution plan model** — Python data structures representing planned activities (not execution itself)
- **Project structure** — Bazel-compatible layout with `o_py_*` rules
- **Documentation scaffolding**

### 2.2 Out of Scope (Future Phases)

- Pipeline execution runtime (local or distributed)
- Advanced query language (XPath/jq-style, SQL embedding)
- Graph pruning and lazy evaluation optimization
- LSP advanced features (types, ports, parameters, diagnostics)
- UI/web dashboard
- Multi-tenant access control

### 2.3 Assumptions

- The `starlarkish` evaluator (`common/python/starlarkish`) is stable and adequate for MVP needs
- `.mlody` file extension is the standard for pipeline definitions
- Users have Python 3.13.2 available (hermetic via rules_python)
- `pygls` is available or can be added as a dependency for the LSP server
- `click` for CLI, `rich` for terminal output (per repo conventions)

### 2.4 Constraints

- Must use existing Bazel build system with `o_py_*` rules
- Must follow repo conventions: absolute imports, ruff formatting, basedpyright strict mode
- Dependencies managed via `o-repin` — no pinned versions in `pyproject.toml`

---

## 3. Stakeholders

| Role | Description | Primary Needs |
|------|-------------|---------------|
| Pipeline Builder | Writes `.mlody` definitions | Authoring experience: LSP support, shell for testing definitions |
| ML Engineer | Queries pipeline values, inspects structure | `mlody show` to access specific values, clear output |
| Platform Engineer | Maintains mlody framework itself | Clean architecture, extensibility, testability |

---

## 4. User Requirements

### 4.1 User Personas

**Persona 1: Pipeline Builder**
- Writes and maintains `.mlody` pipeline definitions
- Needs fast feedback on syntax and structure (LSP, shell)
- Wants familiar Python-like syntax with Starlark's safety guarantees
- May define reusable modules loaded across pipelines

**Persona 2: ML Engineer (Consumer)**
- Queries existing pipeline definitions to understand configuration
- Uses `mlody show` to inspect specific values (hyperparameters, paths, etc.)
- Does not necessarily write `.mlody` files
- Wants simple, predictable target addressing

### 4.2 User Stories

**Epic 1: Pipeline Inspection**

- **US-001:** As an ML Engineer, I want to run `mlody show @team//path/to:target.field` so that I can inspect any value in a pipeline definition.
  - **Acceptance Criteria:**
    - Given a valid target path, when I run `show`, then the resolved value is displayed
    - Given a target path with nested field access, when I run `show`, then the nested field value is returned
    - Given an invalid target path, when I run `show`, then a clear error message is displayed
  - **Priority:** Must Have

- **US-002:** As a Pipeline Builder, I want to run `mlody shell` so that I can interactively explore and test pipeline definitions.
  - **Acceptance Criteria:**
    - Given I launch `mlody shell`, then I get a Python REPL
    - Given I'm in the shell, when I type `show(...)`, then it behaves like the CLI `show` subcommand
    - Given I'm in the shell, then tab completion works for available commands
  - **Priority:** Must Have

**Epic 2: Pipeline Definition**

_This is the bulk of the project — defining the DSL primitives, pipeline graph model, and the Starlark constructs that Pipeline Builders use to express training, evaluation, and batch processing workflows. To be revised after Epic 1 implementation is complete._

- **US-003:** [TBD — Pipeline DSL primitives and graph model]
  - **Priority:** Must Have
  - **Status:** Placeholder — requirements to be elaborated post-Epic 1

**Epic 3: Authoring Support**

- **US-004:** As a Pipeline Builder, I want LSP completion in my `.mlody` files so that I can discover available builtins and loaded symbols.
  - **Acceptance Criteria:**
    - Given I type `struct(`, then I see completion for the `struct` builtin
    - Given I have `load(":helper.mlody", "MY_VAR")`, then `MY_VAR` appears in completions
    - Given I type `builtins.`, then I see `register`, `ctx`, and other available methods
  - **Priority:** Must Have

- **US-005:** As a Pipeline Builder, I want go-to-definition for `load()` targets so that I can navigate between `.mlody` files.
  - **Acceptance Criteria:**
    - Given I invoke go-to-definition on a `load("//path/to/file.mlody", ...)` call, then my editor opens that file
    - Given I invoke go-to-definition on an imported symbol, then my editor navigates to where it's defined in the loaded file
  - **Priority:** Must Have

**Epic 4: Execution Planning**

- **US-006:** As a Platform Engineer, I want mlody to produce an execution plan data structure so that a future executor can consume it.
  - **Acceptance Criteria:**
    - Given a loaded pipeline, when I request a plan, then I get a list of typed activity objects
    - Activity objects are serializable Python dataclasses/structs
    - The plan model is extensible for new activity types
  - **Priority:** Should Have

---

## 5. Functional Requirements

### 5.1 Target Addressing

**FR-001: Bazel-style Target Syntax**
- **Description:** mlody uses a target addressing scheme based on Bazel's label syntax, extended with `@ROOT` for multi-root support.
- **Syntax:**
  - `@ROOT//path/to:target` — fully qualified target in a named root
  - `//path/to:target` — target relative to the default root
  - `:target` — target in the current package/directory
  - Dot-access for nested fields: `@ROOT//path/to:target.field.subfield`
- **Behavior:**
  - `@ROOT` selects a root namespace (e.g., `@TEAM_A`, `@INFRA`)
  - Multiple roots can be loaded simultaneously (a "forest" of graphs)
  - Path resolution follows the starlarkish evaluator's existing `//` and `:` conventions
- **Priority:** Must Have
- **Dependencies:** Starlarkish evaluator's `load()` path resolution

### 5.2 CLI Tool

**FR-002: CLI Entry Point**
- **Description:** `mlody` is a click-based CLI with subcommands. The architecture supports easy addition of new subcommands.
- **Interface:** `mlody [OPTIONS] COMMAND [ARGS]`
- **Global Options:**
  - `--roots FILE` — path to the roots file (default: `mlody/roots.mlody` relative to monorepo root)
  - `--verbose` — increase output verbosity
- **Commands:** `shell`, `show` (extensible)
- **Priority:** Must Have

**FR-014: Monorepo Root Requirement**
- **Description:** `mlody` must be run from the top of the monorepo. On startup, the CLI verifies that the current working directory is the monorepo root by checking for the presence of `MODULE.bazel` (and/or `.git`). If the check fails, mlody exits with a clear error message directing the user to run from the monorepo root.
- **Rationale:** Discourages running from arbitrary subdirectories, which would break `//`-rooted path resolution and lead to confusing errors.
- **Error Message:** e.g., `"Error: mlody must be run from the monorepo root (expected MODULE.bazel in current directory). Please cd to the repo root and try again."`
- **Priority:** Must Have

**FR-003: `show` Subcommand**
- **Description:** Resolves and displays values from loaded pipeline definitions.
- **Interface:** `mlody show TARGET [TARGET...]`
- **Inputs:** One or more Bazel-style target references (FR-001)
- **Processing:**
  1. Load/evaluate necessary `.mlody` files via starlarkish evaluator
  2. Resolve target path to a value in the evaluated graph
  3. Format and display the value
- **Outputs:** Rich-formatted terminal output of the resolved value(s)
- **Error Handling:**
  - Target not found → clear error with suggestion of available targets
  - Evaluation error in `.mlody` file → display file, line, and error message
- **Priority:** Must Have

**FR-004: `shell` Subcommand**
- **Description:** Launches an interactive Python REPL with all mlody commands available as functions at the top level.
- **Interface:** `mlody shell`
- **Behavior:**
  - Starts a `ptpython`-based REPL
  - Pre-populates namespace with mlody commands: `show(...)`, and future commands
  - Supports tab completion for commands and their arguments
  - Shell state persists across commands within a session (evaluated graphs are cached)
- **Extensibility:** Adding a new CLI subcommand should automatically (or with minimal wiring) make it available in the shell
- **Priority:** Must Have (skeleton — shell launches, `show` callable inside)

### 5.3 LSP Server

**FR-005: LSP Server Foundation**
- **Description:** A pygls-based Language Server Protocol server for `.mlody` files.
- **Capabilities (MVP):**
  - `textDocument/completion` — completions for builtins, loaded symbols, `load()` paths
  - `textDocument/definition` — go-to-definition for `load()` file paths and imported symbols
- **File Association:** `.mlody` files
- **Priority:** Must Have (skeleton with these two capabilities)

**FR-006: Completion Provider**
- **Description:** Provides context-aware completions in `.mlody` files.
- **Completion Sources:**
  - Safe builtins (`struct`, `print`, `len`, `range`, etc.)
  - `builtins.*` methods (`register`, `ctx`)
  - Symbols imported via `load()` in the current file
  - File paths in `load()` strings (relative and absolute)
- **Priority:** Must Have

**FR-007: Go-to-Definition Provider**
- **Description:** Navigates to definitions of symbols and files.
- **Targets:**
  - `load("//path/to/file.mlody", ...)` → opens the referenced file
  - Imported symbol name → jumps to its definition in the source file
- **Priority:** Must Have

### 5.4 Workspace & Root Discovery

**FR-011: Roots File**
- **Description:** Roots are defined in a Starlark file (`roots.mlody`) that uses the evaluator itself to register named roots.
- **Convention:** Default file is `mlody/roots.mlody` relative to the monorepo root, overridable via `--roots` CLI flag. Location may change as more `.mlody` files are added — kept as a configurable default.
- **Format:**
  ```starlark
  load("//mlody/core/builtins.mlody", "root")

  root(
      name="lexica",
      path="//mlody/teams/lexica",
      description="text ML team",
  )

  root(
      name="common",
      path="//mlody/common",
      description="shared modules",
  )
  ```
- **Behavior:** Each `root()` call registers a named root with a path and description. The `name` becomes the `@ROOT` identifier used in target addressing.
- **Priority:** Must Have

**FR-012: Two-Phase Loading**
- **Description:** Loading proceeds in two phases to build the full forest of pipeline definitions.
- **Phase 1:** Evaluate `roots.mlody` → collects the set of registered roots (name, path, description).
- **Phase 2:** For each root, discover and evaluate all `.mlody` files found under the root's path. This populates the full graph of pipeline definitions.
- **Result:** The complete forest is available for target resolution, `show`, and plan generation.
- **Future:** Graph pruning to evaluate only what's needed for a given query (out of scope for MVP).
- **Priority:** Must Have

**FR-013: Future Consideration — Team Grouping**
- **Description:** A `team()` primitive could group multiple roots plus metadata (owners, contacts, etc.). Deferred — the flat root list is sufficient for MVP.
- **Priority:** Won't Have (MVP) — noted for future phases

### 5.5 Starlarkish Integration

**FR-008: Python→Starlark Value Injection**
- **Description:** The host (mlody CLI/LSP) can inject values into the starlarkish evaluation namespace, making them available as builtins in `.mlody` files.
- **Mechanism:** Pass a dictionary to the evaluator that gets merged into the sandbox namespace.
- **Value Types:**
  - Ideally: only Starlark primitives (`int`, `float`, `str`, `bool`, `list`, `dict`, `struct`)
  - MVP shortcut: Python objects may be passed with care, documented as non-stable
- **Use Cases:** Injecting environment info, user overrides, organization-level constants
- **Priority:** Must Have

**FR-009: Starlark→Python Value Extraction**
- **Description:** Values registered in `.mlody` files (via `builtins.register()`) are accessible to the host as Python objects.
- **Mechanism:** Already supported by `evaluator.roots` and `evaluator.targets` — mlody wraps this with target resolution (FR-001).
- **Priority:** Must Have (already exists, needs thin wrapper)

### 5.6 Execution Plan Model

**FR-010: Plan Data Model**
- **Description:** Python data structures (dataclasses or similar) representing a planned execution activity.
- **Requirements:**
  - Base class/protocol for all activity types
  - Extensible — new activity types can be added without modifying the base
  - Serializable (at minimum to dict/JSON)
  - Example activity types (stubs): `BuildImage`, `Execute` [TBD — details deferred]
  - **Plan generation must be separate from plan rendering** — the core produces the plan data structure, rendering to terminal (rich) or other formats is a distinct layer
- **Output:** A list of activity objects representing the execution plan
- **Priority:** Should Have (skeleton/interface only)

---

## 6. Non-Functional Requirements

### 6.1 Code Quality
- **NFR-001:** All code passes basedpyright strict mode
- **NFR-002:** All code formatted with ruff
- **NFR-003:** Test coverage for core logic (target resolution, value injection, plan model)
- **NFR-004:** Tests use `o_py_test` (pytest + debugpy auto-injected)

### 6.2 Extensibility
- **NFR-005:** Adding a new CLI subcommand requires minimal boilerplate and automatically surfaces in the shell
- **NFR-006:** LSP capabilities can be added incrementally without restructuring
- **NFR-007:** Plan activity types are open for extension

### 6.3 Developer Experience
- **NFR-008:** Clear error messages with file/line references for `.mlody` evaluation errors
- **NFR-009:** Rich terminal output for `show` (structured, colored where appropriate)

### 6.4 Performance
- **NFR-010:** Evaluated `.mlody` files are cached within a session (already supported by starlarkish)
- **NFR-011:** LSP responds to completion/definition requests within 200ms for typical file sizes

---

## 7. Project Structure

```
mlody/
├── CLAUDE.md              # Project-specific directives for this subtree
├── REQUIREMENTS.md        # This document
├── cli/
│   ├── __init__.py
│   ├── main.py            # click entry point, command registration
│   ├── show.py            # 'show' subcommand implementation
│   ├── shell.py           # 'shell' subcommand (REPL)
│   └── BUILD.bazel
├── lsp/
│   ├── __init__.py
│   ├── server.py          # pygls server setup, capability registration
│   ├── completion.py      # Completion provider
│   ├── definition.py      # Go-to-definition provider
│   └── BUILD.bazel
├── core/
│   ├── __init__.py
│   ├── targets.py         # Target addressing: parsing @ROOT//path:target.field
│   ├── workspace.py       # Workspace discovery, root management, evaluator bridge
│   ├── plan.py            # Execution plan data model
│   └── BUILD.bazel
├── docs/
│   └── ...                # Documentation (future)
└── BUILD.bazel            # Top-level BUILD
```

---

## 8. Integration Requirements

### 8.1 Starlarkish Library
- **Source:** `//common/python/starlarkish`
- **Integration:** Direct Python dependency via Bazel
- **Direction:** Bidirectional
- **Interface:** `Evaluator` class for loading `.mlody` files; namespace injection via constructor or method extension

### 8.2 Editor Integration (LSP)
- **Protocol:** Language Server Protocol (LSP)
- **Transport:** stdio (standard for editor integration)
- **Library:** pygls
- **Target Editors:** Any LSP-compatible editor; NEO (Emacs) is the primary target given the repo

---

## 9. Dependencies

| Dependency | Type | Status | Notes |
|------------|------|--------|-------|
| `starlarkish` evaluator | Internal | Stable | Core evaluation engine |
| `click` | External | Available | CLI framework (repo convention) |
| `rich` | External | Available | Terminal output (repo convention) |
| `pygls` | External | Needs addition | LSP server framework |
| `ptpython` | External | Needs addition | Interactive REPL for `mlody shell` |
| Python 3.13.2 | Toolchain | Available | Hermetic via rules_python |

---

## 10. Open Questions

| ID | Question | Status | Resolution |
|----|----------|--------|------------|
| Q-001 | What is the workspace discovery mechanism? | Resolved | `roots.mlody` at workspace root (conventional), overridable via `--roots` CLI flag |
| Q-002 | Should `mlody show` output support multiple formats? | Resolved | Rich terminal output for MVP. Plan generation is separate from rendering to allow future format options |
| Q-003 | How are roots (`@ROOT`) registered/discovered? | Resolved | Defined in `roots.mlody` Starlark file via `root()` calls (see FR-011) |
| Q-004 | Should the shell use `ptpython`, `IPython`, or stdlib `code`? | Resolved | `ptpython` — best autocompletion out of the box |
| Q-005 | Do `pygls` and `ptpython` need to be added to `pyproject.toml`? | Resolved | Yes — add both, then run `o-repin` |
| Q-006 | What entry point file convention should mlody use? | Resolved | Two-phase: `roots.mlody` defines roots, then all `.mlody` files under each root path are discovered and evaluated (see FR-012) |
| Q-007 | How should workspace root be discovered when `--roots` is not specified? | Resolved | Require CWD to be the monorepo root (verify via `MODULE.bazel`). Default roots path is `mlody/roots.mlody`. Exit with error if not at monorepo root (see FR-014). No directory walking — explicit is better. |

---

## 11. Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-16 | Requirements Analyst AI | Initial draft based on stakeholder discovery |
| 1.1 | 2026-02-16 | Requirements Analyst AI | Resolved Q-001 through Q-006; added FR-011 (roots file), FR-012 (two-phase loading), FR-013 (team grouping — deferred); added ptpython dependency; separated plan generation from rendering |

---

## Implementation Phases

These features must be implemented in order due to dependencies:

### Phase 0: Foundation
- CORE-000: Workspace & starlarkish integration (FR-008, FR-009, FR-011, FR-012)
- CLI-000: CLI framework (FR-002, FR-014, global options, subcommand registration)

### Phase 1: Core Functionality (can be parallel after Phase 0)
- CLI-001: show command (FR-001, FR-003)
- CLI-002: shell command (FR-004)

### Phase 2: Authoring Support (can be parallel after Phase 0)
- LSP-001: Completion (FR-005, FR-006)
- LSP-002: Go-to-definition (FR-007)

### Phase 3: Future
- CORE-001: Execution plan model (FR-010)

**End of Requirements Document**
