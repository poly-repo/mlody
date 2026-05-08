# Requirements Document: mlody/common/config — Workspace Configuration Rule

**Version:** 1.0  
**Date:** 2026-05-07  
**Prepared by:** Requirements Analyst AI  
**Status:** Draft

---

## 1. Executive Summary

Mlody workspaces expose pipeline values whose defaults are baked into the
`.mlody` source files. Today the only mechanism for overriding those defaults at
runtime is the `--with LABEL=VALUE` CLI flag passed to `mlody show` (and
internally consumed by `configure_workspace` in `mlody/resolver/resolver.py`).
This is ergonomic for one-off interactive use but impractical for teams that
need a stable, version-controlled set of workspace-level defaults that differ
from the built-in `default=` values in the source files.

The proposed feature introduces a `config` rule — a new Starlark construct
available in `.mlody` files — that declares a named collection of `setf`-style
value assignments. When the workspace is loaded, each registered `config` is
applied automatically in hierarchical source-tree order (root of the repository
first, deeper packages last), after all source defaults have been evaluated
(Phase 2). The resulting effective values can subsequently be overridden further
by `--with` CLI arguments, establishing a three-tier precedence chain:

```
source default=  <  config rules  <  --with CLI args
```

The expected business value is: teams can commit a canonical `config.mlody` file
per project that encodes environment-specific or experiment-specific overrides
without modifying shared source files and without relying on fragile shell
scripts that re-issue `--with` flags.

---

## 2. Project Scope

### 2.1 In Scope

- A new `config` Starlark rule (via `//mlody/core/rule.mlody`) available in
  `.mlody` files.
- Three attributes on `config`: `name`, `description`, and `rules`.
- The `rules` attribute is a dict mapping label strings (the targets to
  override) to arbitrary literal values (the values to assign).
- Automatic application of all registered `config` objects after workspace load
  (Phase 2 / Phase 3), before `--with` CLI overrides are applied.
- The `config` rule is registered under the `"config"` kind in the evaluator
  registry, following the same pattern as `"type"`, `"location"`, and
  `"representation"`.
- Order-dependent application of `rules` entries within a single `config` (later
  entries win within the same config object).
- Hierarchical application order across multiple `config` objects: a `config`
  declared higher in the source tree (e.g. `//mlody/config.mlody`) is applied
  first and can be overridden by one declared closer to the entity being
  configured (e.g. `//mlody/teams/pixella/config.mlody`). "Later wins" means
  "deeper in the package hierarchy", not "registered later in load order".
- The framework extensibility point for new attributes on `config` is preserved
  by design (the rule mechanism makes adding attributes straightforward in
  future iterations).

### 2.2 Out of Scope (v1)

- Duplicate-key validation within a single `config.rules` dict — silently
  last-writer-wins; a lint/validation pass is deferred.
- Conflict detection across multiple `config` objects (two configs writing the
  same label with different values).
- Named `config` selection (activating one config vs. another at load time); all
  registered configs are applied in v1.
- Conditional application of `config` rules (e.g. environment-based activation).
- Additional attributes beyond `name`, `description`, and `rules`.
- Type validation of rule values against the declared type of the target value
  at `config` definition time (deferred; `setf` already enforces this at
  application time via the resolver pipeline).
- A `config` command on the CLI for listing or inspecting active configs.
- Cross-config dependency ordering (configs depend on each other).

### 2.3 Assumptions

- The `setf` machinery in `mlody/core/setf.py` is the correct and only mechanism
  for applying value overrides after workspace load. The `config` rule delegates
  entirely to `setf` at application time, exactly as `configure_workspace` does
  for `--with` overrides.
- The evaluator's `builtins.register("config", struct)` call — the same pattern
  used for `"type"`, `"location"`, `"representation"` — is the correct
  registration path and is already supported (the evaluator accepts arbitrary
  kind strings).
- The `rules` dict values are arbitrary Starlark literal values (strings,
  integers, floats, booleans, `None`). Struct references are not supported as
  rule values in v1.
- The application of `config` rules happens after Phase 2 (all `.mlody` files
  evaluated) and before `--with` CLI overrides. This maps to the existing
  `WorkspaceLoader.load()` flow: after `resolve_all()` and
  `convert_ports_to_structs()`, and before `configure_workspace` processes
  `--with` entries.
- A `config` in a team-level `.mlody` file (e.g.
  `mlody/teams/lexica/config.mlody`) is discovered and evaluated as part of
  Phase 2's glob, so no special loading is required.
- The `name` and `description` attributes follow the same validation rules as
  all other `rule`-based names in `rule.mlody`: non-empty, `[a-zA-Z0-9_-]` only.

### 2.4 Constraints

- Starlark purity: `config.mlody` must be valid Starlark. No new `python.*`
  escapes without explicit prior approval.
- Build rules: `o_py_library`, `o_py_test`; no raw `py_*` rules.
- `.mlody` files are not currently bundled as Bazel `data` dependencies — this
  deliberate convention must not be changed by this work.
- No modifications to `starlarkish` internals (`common/python/starlarkish/`);
  all changes live in `mlody/`.
- Gazelle manages BUILD files for Go and Python; no manual edits to BUILD files
  for Go or Python targets.

---

## 3. Stakeholders

| Role               | Group                    | Responsibilities                                                        |
| ------------------ | ------------------------ | ----------------------------------------------------------------------- |
| Framework author   | mlody core team          | Implements `config.mlody`; owns the `config` rule and application logic |
| Pipeline authors   | Team/project DSL authors | Write `config(name=..., rules={...})` in team `.mlody` files            |
| Platform operators | Infra/DevEx              | Define workspace-level configuration conventions across teams           |
| Spec writer        | Architecture             | Consumes this document to produce the `config.mlody` spec and tasks     |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Allow teams to declare workspace-level value overrides in
  version-controlled `.mlody` files, eliminating the need to re-issue `--with`
  flags on every CLI invocation.
- **BR-002:** Ensure the override precedence chain (`default=` → `config` rules
  → `--with`) is unambiguous and predictable, so that operators can reason about
  the effective value of any pipeline parameter.
- **BR-003:** Keep the feature additive and non-breaking: workspaces that
  contain no `config(...)` declarations must load and behave identically to
  today.

### 4.2 Success Metrics

- **KPI-001:** A `config(name="team_defaults", rules={":lr": 0.001})` in a team
  `.mlody` file causes `ws.resolve(":lr")` to return a value struct whose
  effective scalar is `0.001` after workspace load, without any `--with` flag.
- **KPI-002:** A subsequent `--with :lr=0.01` applied via `configure_workspace`
  overrides the config-set value to `0.01`, confirming the three-tier precedence
  chain.
- **KPI-003:** All existing `//mlody/...` tests pass unchanged when
  `config.mlody` is introduced.
- **KPI-004:** A workspace with no `config(...)` declarations loads with
  identical results to today (zero regression).

---

## 5. User Requirements

### 5.1 User Personas

**Persona 1: Pipeline Author / Team Lead**

- Maintains a `config.mlody` for their project.
- Wants to encode experiment-specific hyperparameter defaults (e.g.
  `learning_rate`, `batch_size`) that differ from the built-in `default=` values
  but should be shared across the whole team.
- Pain point today: must either modify source `default=` values (polluting
  shared definitions) or re-issue `--with` flags on every invocation (error-
  prone, not version-controlled).
- Needs: a terse, readable dict-based override syntax in a standard location per
  project.

**Persona 2: Platform Operator**

- Manages infrastructure-level overrides (e.g. storage paths, executor
  endpoints) that differ per environment (dev vs. prod).
- Wants a config per environment checked into the repo, activated at deploy
  time.
- Needs: multiple `config` declarations with a predictable order of application.

### 5.2 User Stories

**Epic 1: Declare workspace overrides in a config file**

- **US-001:** As a pipeline author, I want to write
  `config(name="team_defaults", rules={":learning_rate": 0.001, ":batch_size": 32})`
  in my team's `config.mlody` so that every collaborator who loads the workspace
  gets the team's agreed defaults without extra flags.
  - Acceptance Criteria: Given the above config declaration in any `.mlody` file
    under the team root, when the workspace is loaded, then
    `ws.resolve(":learning_rate")` returns a value struct whose effective scalar
    equals `0.001`.
  - Priority: Must Have

- **US-002:** As a pipeline author, I want to be able to give my config a
  human-readable description so that collaborators understand the intent of the
  overrides.
  - Acceptance Criteria:
    `config(name="...", description="Experiment-7 hyperparameters", rules={...})`
    evaluates without error and the registered config struct has
    `description == "Experiment-7 hyperparameters"`.
  - Priority: Should Have

- **US-003:** As a platform operator, I want later `--with` CLI overrides to
  take precedence over `config` rules so that I can still tweak individual
  values interactively without modifying the config file.
  - Acceptance Criteria: Given `config(name="defaults", rules={":lr": 0.001})`
    and a subsequent `--with :lr=0.01`, the resolved value of `:lr` is `0.01`.
  - Priority: Must Have

- **US-004:** As a pipeline author, I want a workspace with no `config`
  declarations to behave identically to today so that adopting this feature is
  strictly opt-in and non-breaking.
  - Acceptance Criteria: All existing tests pass; no existing `.mlody` file
    requires modification.
  - Priority: Must Have

**Epic 2: Multiple configs and ordering**

- **US-005:** As a platform operator, I want configs defined closer to the
  entity (deeper in the package hierarchy) to take precedence over configs
  defined at higher levels, so that organization-wide defaults can be layered
  under team-level overrides and project-level overrides without manual
  ordering.
  - Acceptance Criteria: Given two config objects both setting `:lr` — one in
    `//mlody/config.mlody` (shallower, applied first) setting it to `0.001`, and
    one in `//mlody/teams/pixella/config.mlody` (deeper, applied later) setting
    it to `0.0001` — the resolved value is `0.0001` (deeper package wins).
  - Priority: Must Have

---

## 6. Functional Requirements

### 6.1 The `config` Rule

**FR-001: `config` rule definition in `mlody/common/config.mlody`**

- Description: A new Starlark `rule` (via `//mlody/core/rule.mlody`) named
  `config`. Its implementation function (`_config_impl`) constructs a config
  struct and registers it in the evaluator under kind `"config"`.
- File location: `mlody/common/config.mlody`.
- Attrs accepted by the rule:

  | Attr name     | Type     | Mandatory | Default | Notes                                                     |
  | ------------- | -------- | --------- | ------- | --------------------------------------------------------- |
  | `name`        | `string` | Yes       | —       | Identifier for this config; follows `[a-zA-Z0-9_-]` rules |
  | `description` | `string` | No        | `""`    | Human-readable explanation of the config's purpose        |
  | `rules`       | `dict`   | Yes       | —       | Mapping of label strings to literal override values       |

- Processing in `_config_impl`:
  1. Validate `name` (non-empty, allowed characters — same as `rule.mlody`
     validation).
  2. Validate that `rules` is a dict.
  3. Validate that every key in `rules` is a string (a label or name). Values
     may be any Starlark literal (string, int, float, bool, `None`).
  4. Construct a config struct:
     `struct(kind="config", name=ctx.attr.name, description=ctx.attr.description, rules=ctx.attr.rules)`.
  5. Call `builtins.register("config", config_struct)` to register.
  6. Return the config struct.
- Priority: Must Have

**FR-002: Config struct shape**

Every config struct produced by the `config` rule has exactly these fields:

| Field         | Type     | Description                                                          |
| ------------- | -------- | -------------------------------------------------------------------- |
| `kind`        | `string` | Always `"config"` — discriminator                                    |
| `name`        | `string` | Config identifier                                                    |
| `description` | `string` | Human-readable description (empty string when not provided)          |
| `rules`       | `dict`   | Ordered mapping of label strings → arbitrary literal override values |

- The `rules` dict preserves Starlark dict iteration order. Entries are applied
  in insertion order; later entries overwrite earlier entries that target the
  same label.
- Priority: Must Have

**FR-003: Registration under `"config"` kind**

- Description: `builtins.register("config", config_struct)` stores the config
  struct in the evaluator registry under kind `"config"`. The evaluator already
  supports arbitrary kind strings; no evaluator change is required.
- Multiple `config(...)` declarations across one or more `.mlody` files each
  register separately, preserving the order in which they were evaluated during
  Phase 2.
- `builtins.lookup("config", name)` must return the registered config struct by
  name.
- Priority: Must Have

### 6.2 Automatic Application After Workspace Load

**FR-004: Apply all registered configs after workspace load**

- Description: After Phase 2 (all `.mlody` files evaluated and `resolve_all()` /
  `convert_ports_to_structs()` complete) and before `--with` CLI overrides are
  applied, the workspace loader retrieves all registered `"config"` objects from
  the registry and applies each one's `rules` dict in order using `setf`.
- Application is performed by the Python host (not in Starlark), mirroring the
  logic in `configure_workspace` (`mlody/resolver/resolver.py`):

  ```python
  for config_struct in registry.get_all("config"):
      for label, value in config_struct.rules.items():
          setf(label, value, workspace=workspace, source=f"DEFAULT: {name}.rules")
  ```

  The `source` string follows the existing lineage convention
  (`"COMMAND_LINE: ..."`, `"DEFAULT: ..."`) so that override provenance is
  visible in `mlody show --verbose` or lineage inspection.

- Priority: Must Have

**FR-005: Precedence chain enforcement**

- Description: The three-tier override chain must be respected:
  1. Source `default=` values are set during Phase 2 evaluation.
  2. `config` rules are applied after Phase 2, overwriting source defaults.
  3. `--with` CLI overrides are applied last (by `configure_workspace`),
     overwriting config-rule-set values.
- This is achieved naturally by the application order: config rules run before
  `configure_workspace` is called; no special conflict-resolution logic is
  required.
- Priority: Must Have

**FR-006: Hierarchical application order within and across configs**

- Description: Within a single `config.rules` dict, entries are applied in
  insertion order. If two entries target the same label, the later entry wins
  (no error is raised — duplicate-key validation is deferred to a future
  release).
- Across multiple `config` objects, configs are applied in **hierarchical
  source-tree order**: a `config` defined higher in the package tree is applied
  first; a `config` defined closer to the entity being configured (deeper in the
  hierarchy) is applied later and therefore wins. For example,
  `//mlody/config.mlody` (organization-wide defaults) is applied before
  `//mlody/teams/pixella/config.mlody` (team-level overrides), which is applied
  before any project-level `config` even deeper in the tree.
- This is the key design intent: "later wins" means "deeper in the package
  hierarchy / closer to the entity being configured", not "registered later in
  load order." The Phase 2 glob produces files in sorted path order, which
  naturally reflects this hierarchical nesting (shallower paths sort before
  deeper paths).
- Priority: Must Have

### 6.3 Lineage / Source Attribution

**FR-007: Lineage source string for config-applied values**

- Description: Values modified by a `config` rule must carry a lineage event
  with a source string that identifies the config by name and the specific label
  that was set. The format follows the existing conventions:

  ```
  DEFAULT: <config_name>.<label>
  ```

  This mirrors the `"DEFAULT: ..."` convention used for `default=` values and
  the `"COMMAND_LINE: ..."` convention used for `--with` overrides.

- Priority: Should Have
- Dependencies: FR-004; existing lineage system in `mlody/core/lineage.py`.

### 6.4 Python Host Representation

**FR-008: Python `Config` dataclass in `mlody/common/config.py`**

- Description: A Python `@dataclass` named `Config` must be created at
  `mlody/common/config.py`, following the same pattern as `task.py`,
  `action.py`, and other entity files in the same directory. It mirrors the
  Starlark struct fields and provides the Python host with a typed, importable
  representation of a registered config.
- Fields:

  | Field         | Python type | Notes                                                       |
  | ------------- | ----------- | ----------------------------------------------------------- |
  | `name`        | `str`       | Config identifier; matches the Starlark struct `name` field |
  | `description` | `str`       | Human-readable description; empty string when not provided  |
  | `rules`       | `dict`      | Ordered mapping of label strings → literal override values  |

- The class must implement `populate_from_struct(struct)` (or the equivalent
  class/static method used by `task.py`, `action.py`, etc. in the same
  directory) to construct a `Config` instance from the Starlark `Struct`
  returned by `builtins.lookup("config", name)`.
- If the codebase pattern for the sibling files also includes a `to_struct()`
  method (or equivalent conversion back to a Starlark `Struct`), `Config` must
  implement it as well for consistency.
- The `WorkspaceLoader` config-application logic (FR-004) must use `Config`
  instances (populated via `populate_from_struct`) rather than raw Starlark
  struct attribute access.
- Priority: Must Have
- Dependencies: FR-002 (config struct shape); `mlody/common/config.mlody`
  (Starlark counterpart).

---

## 7. Non-Functional Requirements

### 7.1 Consistency with Existing Rule Pattern

- The code structure, naming conventions, and docstring style of `config.mlody`
  must mirror `locations.mlody` and `representation.mlody`. A reader familiar
  with the type/location system must immediately orient in the config module.

### 7.2 Immutability

- Config structs are Starlark `Struct` instances (immutable). No mutable state
  is introduced in the Starlark layer.

### 7.3 Zero-Cost for Workspaces Without Configs

- If the registry contains no `"config"` entries after Phase 2, the post-load
  application step is a no-op. No measurable overhead is introduced for existing
  workspaces.

### 7.4 Starlark Purity

- No new `python.*` escapes in `config.mlody` unless explicitly approved. Every
  `python.*` usage must be marked with a comment for the audit grep.

### 7.5 Backward Compatibility

- All existing `.mlody` files that do not contain `config(...)` declarations
  must continue to evaluate without error or behaviour change.

---

## 8. Data Requirements

### 8.1 Config Struct Shape

See FR-002. The shape mirrors the pattern established by `location` and
`representation` structs.

### 8.2 Rules Dict Values

- Keys: non-empty strings representing mlody labels (e.g. `:learning_rate`,
  `@lexica//models:lr`).
- Values: arbitrary Starlark literals. The concrete type is not validated
  against the target value's declared `type` at `config` definition time; `setf`
  enforces type compatibility at application time.

### 8.3 Data Retention

- Config structs are in-memory Starlark values for the lifetime of a workspace
  evaluation session. They are not persisted.

### 8.4 Data Privacy & Compliance

- No data privacy implications. Config rules contain only pipeline parameter
  overrides, not user data or credentials.

---

## 9. Integration Requirements

### 9.1 Internal Module Dependencies

| Module                                 | Change required                                                                                                |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `mlody/common/config.mlody`            | New file: declares the `config` Starlark rule                                                                  |
| `mlody/common/config.py`               | New file: Python `@dataclass` `Config` mirroring the Starlark struct; includes `populate_from_struct` (FR-008) |
| `mlody/core/workspace_loader.py`       | Extended to retrieve all `"config"` structs, populate `Config` instances, and apply them post-Phase 2 (FR-004) |
| `mlody/resolver/resolver.py`           | `configure_workspace` unchanged — `config` application occurs before it is called                              |
| `mlody/core/setf.py`                   | Used as-is for applying each `config.rules` entry                                                              |
| `common/python/starlarkish/evaluator/` | No changes required — arbitrary kind strings are already supported                                             |

### 9.2 Load Order

`config.mlody` is a standard `.mlody` file. It does not need to be loaded during
Phase 1 (root discovery). It is evaluated as part of Phase 2 when discovered by
the glob. The `config` rule itself is made available in the sandbox through the
same mechanism as `typedef`, `location`, etc. (either auto-loaded or explicitly
`load()`-ed in team files — see OQ-001).

### 9.3 API Requirements

- No HTTP or external APIs. All changes are internal to the mlody evaluation and
  workspace load path.

---

## 10. User Interface Requirements

### 10.1 DSL Syntax

The `config` rule is available in any `.mlody` file:

```starlark
load("//mlody/common/config.mlody")

config(
    name        = "team_defaults",
    description = "Shared hyperparameter defaults for Project Lexica.",
    rules = {
        ":learning_rate": 0.001,
        ":batch_size":    32,
        ":optimizer":     "adamw",
        ":max_epochs":    100,
    },
)
```

Multiple configs in a file or across files are valid:

```starlark
# Base defaults
config(
    name  = "base_defaults",
    rules = {":lr": 0.01, ":epochs": 10},
)

# Environment-specific overlay (applied after base_defaults)
config(
    name  = "prod_overrides",
    rules = {":lr": 0.001},
)
```

### 10.2 Error Message Standards

Error messages follow the existing codebase pattern:

```
config 'team_defaults', rule ':learning_rate': setf failed — <reason from setf>
```

---

## 11. Reporting & Analytics Requirements

Not applicable for this feature.

---

## 12. Security & Compliance Requirements

### 12.1 No Credential Storage

Config `rules` values must not be used to store secrets or credentials. This is
a convention requirement; no enforcement mechanism is introduced in v1.

### 12.2 Audit Trail

The lineage source string (FR-007) provides a basic audit trail: any value
modified by a `config` rule is labelled `"DEFAULT: <config_name>.<label>"` in
its lineage, making it distinguishable from source defaults and CLI overrides.

---

## 13. Infrastructure & Deployment Requirements

### 13.1 File Layout

```
mlody/common/config/
└── REQUIREMENTS.md             # This document

mlody/common/
├── config.mlody                # NEW: declares the config Starlark rule
└── config.py                   # NEW: Python @dataclass Config (FR-008)
```

Additionally, team-level config files continue to live under:

```
mlody/teams/<team>/config.mlody # EXISTING: teams will use config() here
```

### 13.2 BUILD File

`.mlody` files are not currently bundled as Bazel `data` dependencies — this is
the deliberate current state of the project. `config.mlody` follows the same
convention: no BUILD `data` entry is required or added.

### 13.3 Test Target

Tests follow the pattern established by `types_test.py` and `locations_test.py`:

```python
o_py_test(
    name = "config_test",
    srcs = ["config_test.py"],
    deps = [
        "//common/python/starlarkish/evaluator",
        "//mlody/core:workspace",
    ],
)
```

---

## 14. Testing & Quality Assurance Requirements

### 14.1 Testing Pattern

Tests must follow the `types_test.py` / `locations_test.py` pattern:

1. Read real `.mlody` sources at module import time (before any pyfakefs fixture
   activates) using `Path(__file__).parent / "..."`.
2. Build a `_BASE_FILES` dict covering `rule.mlody`, `attrs.mlody`,
   `types.mlody`, `locations.mlody`, and `config.mlody`.
3. Define a helper `_eval(extra_mlody)` that loads `config.mlody` and evaluates
   the extra snippet via `InMemoryFS` + `Evaluator`.
4. For application tests, use a `Workspace` instance and inspect resolved values
   after load.

### 14.2 Required Test Cases

| ID     | Description                                                                                                                                       | Priority    |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| TC-001 | `config(name="x", rules={})` registers a struct with `kind="config"`, `name="x"`, `rules={}`                                                      | Must Have   |
| TC-002 | `config(name="x", description="d", rules={})` registers with `description=="d"`                                                                   | Must Have   |
| TC-003 | `config` without `name` raises a `ValueError` (mandatory attr)                                                                                    | Must Have   |
| TC-004 | `config` without `rules` raises a `ValueError` (mandatory attr)                                                                                   | Must Have   |
| TC-005 | `builtins.lookup("config", "x")` returns the registered config struct                                                                             | Must Have   |
| TC-006 | Rules dict with string, integer, float, boolean, and `None` values all accepted                                                                   | Must Have   |
| TC-007 | Post-load application: a config setting `:lr` to `0.001` causes resolved value to equal `0.001`                                                   | Must Have   |
| TC-008 | Precedence: `config` overrides source `default=`; `--with` overrides the config-set value                                                         | Must Have   |
| TC-009 | Hierarchical order: a config at a deeper package path overrides one at a shallower path when both set the same label                              | Must Have   |
| TC-010 | Order within a single `rules` dict: later entry for same key overwrites earlier entry                                                             | Must Have   |
| TC-011 | Workspace with no `config` declarations loads identically to today (regression)                                                                   | Must Have   |
| TC-012 | Lineage source string for a config-applied value contains `"DEFAULT: <config_name>"`                                                              | Should Have |
| TC-013 | `config` with a non-string key in `rules` raises a `ValueError`                                                                                   | Should Have |
| TC-014 | All existing `//mlody/...` tests pass without modification after introducing `config.mlody`                                                       | Must Have   |
| TC-015 | `Config.populate_from_struct(struct)` correctly populates `name`, `description`, and `rules` from a Starlark struct produced by the `config` rule | Must Have   |
| TC-016 | `WorkspaceLoader` uses `Config` instances (not raw struct attribute access) when applying config rules post-Phase 2                               | Must Have   |

### 14.3 Acceptance Criteria

The `config.mlody` implementation is accepted when all TC-001 through TC-011 and
TC-015 through TC-016 tests pass and the lint check
`bazel build --config=lint //mlody/...` is clean. TC-012 through TC-014 must
also pass before the feature is declared production-ready.

---

## 15. Training & Documentation Requirements

### 15.1 Inline Documentation

- `config.mlody` must carry a module-level docstring explaining the purpose of
  the `config` rule and linking the three-tier precedence chain.
- Each attribute (`name`, `description`, `rules`) must be documented in the
  rule's `attr()` call comments.

### 15.2 Sandbox Table Update

The `mlody/CLAUDE.md` sandbox table must be updated to list `config` as an
available symbol once `config.mlody` is loaded.

### 15.3 Team-Level Guidance

A short note in `mlody/README.md` (or a new `mlody/docs/` entry) explaining the
three-tier override chain and the conventional location for team config files
(`mlody/teams/<team>/config.mlody`).

---

## 16. Risks & Mitigation Strategies

| Risk ID | Description                                                                                                                                                                      | Impact | Probability | Mitigation                                                                                                                                   | Owner        |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| R-001   | `WorkspaceLoader.load()` currently has no hook for post-Phase-2 actions beyond `convert_ports_to_structs()`; adding config application may require refactoring the load sequence | Medium | Medium      | Add a `after_phase2` hook or inline the config application step directly after `convert_ports_to_structs()`                                  | mlody core   |
| R-002   | Config rule values that are Starlark structs (e.g. a `location` struct) passed as rule values: `setf` may not handle non-scalar values gracefully for all target types           | Low    | Low         | Document that `rules` values are restricted to Starlark scalars in v1; add a validation step if structs cause issues at application time     | mlody core   |
| R-003   | Phase 2 glob order is deterministic (sorted) but may vary across OS or Bazel configurations, making multi-config precedence unpredictable                                        | Medium | Low         | Document that config application order follows Phase 2 glob discovery order; teams should use a single config per root to avoid ambiguity    | Architecture |
| R-004   | Duplicate-key suppression in Starlark dicts (Starlark may reject duplicate keys at parse time rather than silently keeping the last one)                                         | Low    | Low         | Verify Starlark dict literal semantics; if parse-time rejection occurs, document that duplicate-key avoidance is the author's responsibility | mlody core   |
| R-005   | `config` name collides with a Python builtin or an existing mlody symbol, causing confusion in `.mlody` files                                                                    | Low    | Low         | The name `config` is not currently in the mlody sandbox; verify via grep before shipping                                                     | mlody core   |

---

## 17. Dependencies

| Dependency                                           | Type     | Status   | Impact if Delayed                                            | Owner       |
| ---------------------------------------------------- | -------- | -------- | ------------------------------------------------------------ | ----------- |
| `//mlody/core/rule.mlody` — `rule`                   | Internal | Stable   | None — rule.mlody is already used by locations, etc.         | mlody core  |
| `//mlody/common/attrs.mlody` — `attr`                | Internal | Stable   | None — attrs.mlody already exists                            | mlody core  |
| `mlody/core/setf.py` — `setf`                        | Internal | Stable   | Cannot apply config rules without `setf`                     | mlody core  |
| `mlody/core/workspace_loader.py` — post-Phase 2 hook | Internal | Required | Config application cannot be triggered automatically         | mlody core  |
| `mlody/resolver/resolver.py` — `configure_workspace` | Internal | Stable   | `--with` override precedence relies on config applying first | mlody core  |
| Evaluator `"config"` kind registry support           | Internal | Stable   | Already supports arbitrary kind strings                      | starlarkish |

---

## 18. Open Questions & Action Items

| ID     | Question / Action                                                                                                                                                                                                               | Owner        | Target Date | Status   |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | ----------- | -------- |
| OQ-001 | Should `config.mlody` be auto-loaded as part of the Phase 1 standard sandbox (like `types.mlody` and `mm.mlody`) so that `config()` is available everywhere without an explicit `load()`, or must teams `load()` it explicitly? | mlody core   | TBD         | Open     |
| OQ-002 | Where exactly in `WorkspaceLoader.load()` should config application be inserted — between `convert_ports_to_structs()` and returning, or as a new `_phase3_config_application()` method?                                        | mlody core   | TBD         | Open     |
| OQ-003 | Should the `rules` dict support mlody label strings with wildcard expansion (e.g. `":cfg_*"`) as keys, consistent with `--with` wildcard support in `configure_workspace`?                                                      | mlody core   | Future      | Deferred |
| OQ-004 | Should the lineage source string format be `"DEFAULT: <config_name>.<label>"` or `"CONFIG: <config_name>: <label>"` to distinguish from the existing `"DEFAULT: ..."` used for `default=` values?                               | mlody core   | TBD         | Open     |
| OQ-005 | When a `config.rules` value fails type coercion in `setf` (e.g. `":epochs": "not_an_int"` for an `integer()` value), should the error abort the entire workspace load or be collected and reported at the end?                  | mlody core   | TBD         | Open     |
| OQ-006 | Should `builtins.lookup("config", name)` be needed by user files, or is the registry lookup purely for internal use by the Python host during application? If user-facing, document it in the sandbox table.                    | mlody core   | TBD         | Open     |
| OQ-007 | Future: should a `config` be activatable by name (e.g. `ws.load(active_config="prod_overrides")`) to support environment-specific activation without touching the source files? Deferred.                                       | Architecture | Future      | Deferred |

---

## 19. Revision History

| Version | Date       | Author                  | Changes                                                                                                                                                                                                                                                              |
| ------- | ---------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-05-07 | Requirements Analyst AI | Initial draft                                                                                                                                                                                                                                                        |
| 1.1     | 2026-05-07 | Requirements Analyst AI | FR-006: corrected application order to hierarchical (deeper wins, not registration order). FR-008: added Python `Config` `@dataclass` requirement for `mlody/common/config.py`. Updated US-005, TC-009, Section 9.1, Section 13.1, and testing criteria accordingly. |

---

## Appendices

### Appendix A: Glossary

| Term                  | Definition                                                                                                                                                |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Config rule           | The Starlark `rule` named `config` defined in `config.mlody`; creates a config struct and registers it under kind `"config"`.                             |
| Config struct         | The immutable Starlark `Struct` produced by a `config(...)` call; carries `kind`, `name`, `description`, and `rules`.                                     |
| `Config` dataclass    | The Python `@dataclass` in `mlody/common/config.py` that mirrors the Starlark config struct; used by the Python host to hold config data in a typed form. |
| Rules dict            | The `rules` attribute on a config struct; an ordered dict mapping label strings to literal override values.                                               |
| Three-tier precedence | The override chain: source `default=` < `config` rules < `--with` CLI args. Each tier overwrites the previous tier's value.                               |
| `setf`                | The Python function in `mlody/core/setf.py` that applies a value override to a workspace target, respecting type constraints and updating lineage.        |
| `configure_workspace` | The Python function in `mlody/resolver/resolver.py` that applies `--with` CLI overrides by calling `setf` for each override entry.                        |
| Phase 2               | The second phase of workspace loading (`WorkspaceLoader._phase2_full_evaluation`): globs all `**/*.mlody` under registered roots and evaluates each file. |
| Lineage               | The provenance trail attached to each value modification, recording the source string (`"DEFAULT: ..."`, `"COMMAND_LINE: ..."`) that caused the change.   |

### Appendix B: Three-Tier Precedence Illustration

```
Source file (values.mlody or team pipeline file):
  value(name="lr", type=float(), default=0.01)       ← tier 1: source default

config.mlody (team config file, evaluated in Phase 2):
  config(name="team", rules={":lr": 0.001})          ← tier 2: config rule
  → applied after Phase 2, overwrites 0.01 → 0.001

CLI invocation:
  mlody show --with :lr=0.0001 @lexica//models:lr    ← tier 3: --with override
  → applied by configure_workspace, overwrites 0.001 → 0.0001
```

### Appendix C: Config Struct Shape

```
Config struct (config.mlody):
  kind        = "config"
  name        = "team_defaults"
  description = "Shared hyperparameters for Project Lexica."
  rules       = {
      ":learning_rate": 0.001,
      ":batch_size":    32,
      ":optimizer":     "adamw",
  }
```

### Appendix D: Comparison with Analogous Rule-Based Modules

| Aspect             | `location` (locations.mlody)           | `representation` (representation.mlody) | `config` (config.mlody)                                |
| ------------------ | -------------------------------------- | --------------------------------------- | ------------------------------------------------------ |
| Registry kind      | `"location"`                           | `"representation"`                      | `"config"`                                             |
| Rule name          | `location`                             | `representation`                        | `config`                                               |
| Key attributes     | `name`, `description`, `base`, `attrs` | `name`                                  | `name`, `description`, `rules`                         |
| Injected symbol?   | Yes (`s3()`, `posix()`, etc.)          | Yes (`json`)                            | No (no factory needed; `config(...)` is used directly) |
| Post-load action?  | No                                     | No                                      | Yes — rules are applied via `setf` after Phase 2       |
| Host Python change | `Evaluator._register` for `"location"` | None                                    | `WorkspaceLoader.load()` extended to apply configs     |

---

**End of Requirements Document**
