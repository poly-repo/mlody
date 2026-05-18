# Requirements Document: action.implementation — Structured Implementation Kinds

**Version:** 1.0  
**Date:** 2026-04-15  
**Prepared by:** Requirements Analyst AI  
**Status:** Draft

---

## 1. Executive Summary

Mlody pipeline actions currently declare how they are executed via
`implementation: string_list` — a flat list of command tokens. This is
sufficient for trivial cases but carries no structural meaning: there is no way
to distinguish a container invocation from a shell script from a system binary
at the DSL level. The executor must infer execution semantics from opaque
strings, which prevents static validation, IDE tooling, and richer execution
backends.

This document specifies a replacement: `action.implementation` becomes a single
structured _implementation object_. The object is one of three named kinds —
`container`, `shell_script`, or `system_binary` — each declared inline inside
the enclosing `action(...)` call. A companion `build_ref` kind system (initially
a single `bazel(target=...)` kind) provides a structured reference to
build-system artifacts consumed by `container`.

The design follows the established pattern of the `location` rule: a
registered-kind system with inline factory calls and validated struct output.
Success is defined as: a spec writer can produce `implementation.mlody` and
`build_ref.mlody` without returning to stakeholders for clarification on any
topic covered below, and the resulting implementation passes a test suite
modelled on `locations_test.py`.

---

## 2. Project Scope

### 2.1 In Scope

- A new `implementation` rule and its three kinds: `container`, `shell_script`,
  `system_binary`
- A new `build_ref` kind system with one predefined kind: `bazel`
- Both implemented as Starlark (`.mlody`) files, likely
  `mlody/common/implementation.mlody` and `mlody/common/build_ref.mlody`
- Inline-only use of all kinds — no top-level named declarations for
  `container`, `shell_script`, `system_binary`, or `bazel`
- Validation of all attributes at `action(...)` evaluation time
- Modification of `action.mlody` to replace the `implementation: string_list`
  attribute with `implementation: implementation_ref` (a struct reference)
- Evaluator registration under new kinds `"implementation"` and `"build_ref"`
- Factory injection for all kinds (`container()`, `shell_script()`,
  `system_binary()`, `bazel()`) into the Starlark sandbox

### 2.2 Out of Scope

- Named top-level declarations of implementation or build_ref objects (unlike
  `location` kinds, which benefit from named reuse, implementations are expected
  to be unique to each action)
- Volume mounts, environment variables, working directory, resource limits, and
  credentials — these remain on the enclosing `action` as config, not on the
  implementation object
- Any build-ref kind other than `bazel` (e.g., Docker Hub image references,
  arbitrary OCI registries, pip packages)
- Abstract base kinds for the implementation hierarchy
- Execution semantics — implementation objects are declarative structs only; how
  the executor actually invokes a container or runs a script is out of scope
- Multiple implementations per action (one implementation object per action)
- An `implementation` rule that supports top-level named registration (the
  `implementation` rule is an internal meta-rule, not user-facing in the same
  way `location` is)
- `args` / argument passing on implementation kinds — arguments are described at
  the `action` level via `named_arg`, `positional_arg`, and
  `environment_variable` location kinds

### 2.3 Assumptions

- `implementation.mlody` and `build_ref.mlody` will both
  `load("//mlody/common/attrs.mlody", ...)` for `attr`, `extend_attrs`, and
  related utilities — consistent with the existing `locations.mlody` pattern.
- The `rule` function from `//mlody/core/rule.mlody` is the correct mechanism
  for defining the meta-rules.
- `builtins.register` and `builtins.inject` are available and follow the same
  protocol as used by `locations.mlody`.
- Starlark `None` equality uses `==`/`!=` (not `is`/`is not`).
- The `python.*` namespace is available for the same operations used in
  `types.mlody` and `locations.mlody`.
- `shell_script` `content` (inline string) and `file` (path reference) are
  mutually exclusive at validation time; the validator enforces exactly one.

### 2.4 Constraints

- All files must be valid Starlark (eventual pure-Starlark goal). New `python.*`
  usage requires explicit approval.
- No raw Bazel `py_*` rules; use `o_py_library`, `o_py_test`.
- Attribute redeclaration in child kind definitions follows the same strict
  no-redeclaration rule as `extend_attrs`.
- `system_binary` accepts only an absolute path for the binary; relative paths
  and Bazel label strings are not valid (those would be `container` with a
  `bazel(...)` build ref).

---

## 3. Stakeholders

| Role             | Group            | Responsibilities                                                    |
| ---------------- | ---------------- | ------------------------------------------------------------------- |
| Framework author | mlody core team  | Implements `implementation.mlody`, `build_ref.mlody`, action change |
| Pipeline authors | Team DSL authors | Write `action(implementation=container(...))` in `.mlody` files     |
| Spec writer      | Architecture     | Consumes this document to produce the implementation spec           |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Allow the execution backend to determine _how_ to run an action at
  pipeline-load time from structural information rather than opaque strings.
- **BR-002:** Enable IDE tooling (LSP completions, validation) to provide
  kind-specific attribute hints inside `container(...)`, `shell_script(...)`,
  and `system_binary(...)`.
- **BR-003:** Provide a clear, extensible pattern so additional implementation
  kinds can be added in future releases without breaking existing pipelines.

### 4.2 Success Metrics

- **KPI-001:** All three implementation kinds and the `bazel` build-ref kind are
  exercisable via their factory functions in `.mlody` files.
- **KPI-002:** `action(implementation=shell_script(content="...", file="..."))`
  raises a validation error at evaluation time (mutual-exclusivity enforced).
- **KPI-003:** Existing pipeline `.mlody` files that used `implementation` as a
  `string_list` produce a clear migration error rather than silent misbehaviour.
- **KPI-004:** Test coverage follows the `locations_test.py` pattern; all tests
  pass under `bazel test //mlody/common:implementation_test`.

---

## 5. User Requirements

### 5.1 User Personas

**Persona 1: Pipeline Author**

- Declares how each action executes: via a container, a shell script, or a
  system tool.
- Needs: terse, self-documenting factory syntax that is familiar from the
  existing `location` / `typedef` patterns.
- Pain point today: `implementation = ["docker", "run", "my-image"]` carries no
  structural meaning; attributes cannot be validated at load time.

**Persona 2: Framework / Executor Author**

- Reads the implementation struct from an action to decide how to invoke it.
- Needs: a well-defined, introspectable struct with a discriminating `kind`
  field so dispatch is unconditional.
- Pain point today: must parse and interpret string lists, which are fragile and
  vary between teams.

### 5.2 User Stories

**Epic 1: Container execution**

- **US-001:** As a pipeline author, I want to write
  `implementation=container(build=bazel(target="//my/image:target"))` so that
  the executor knows to build and run a container image from a Bazel target.
  - Acceptance Criteria: `container(...)` returns a struct with
    `kind="implementation"`, `type="container"`, and a `build` field holding a
    `bazel` build-ref struct.
  - Priority: Must Have

**Epic 2: Shell script execution**

- **US-003:** As a pipeline author, I want to write
  `implementation=shell_script(content="#!/bin/bash\necho hello")` to embed a
  script inline.
  - Acceptance Criteria: `shell_script(content=...)` is valid; `interpreter` is
    optional; `file` must be absent.
  - Priority: Must Have

- **US-004:** As a pipeline author, I want to write
  `implementation=shell_script(file="scripts/run.sh")` to reference a script
  file in the repository.
  - Acceptance Criteria: `shell_script(file=...)` is valid; `content` must be
    absent.
  - Priority: Must Have

- **US-005:** As a pipeline author, I want the system to raise an error if I
  provide both `content` and `file` so that ambiguous declarations are caught
  early.
  - Acceptance Criteria: `shell_script(content="...", file="...")` raises
    `ValueError` at evaluation time.
  - Priority: Must Have

**Epic 3: System binary execution**

- **US-006:** As a pipeline author, I want to write
  `implementation=system_binary(path="/usr/bin/ffmpeg")` to invoke a binary that
  is pre-installed on the execution host.
  - Acceptance Criteria: `system_binary(path=...)` returns a struct with
    `kind="implementation"`, `type="system_binary"`, and a `path` field
    containing the absolute path.
  - Priority: Must Have

- **US-007:** As a pipeline author, I want the system to reject a relative or
  Bazel-label path in `system_binary(path=...)` so that only unambiguous
  absolute paths are used.
  - Acceptance Criteria: `system_binary(path="relative/path")` raises
    `ValueError`. `system_binary(path="//my:target")` raises `ValueError`.
  - Priority: Must Have

**Epic 4: Build references**

- **US-008:** As a pipeline author, I want `bazel(target="//some:target")` to
  produce a structured build-ref struct so the executor can invoke the correct
  Bazel build before launching a container.
  - Acceptance Criteria: `bazel(target=...)` returns a struct with
    `kind="build_ref"`, `type="bazel"`, and a `target` field holding the label
    string.
  - Priority: Must Have

---

## 6. Functional Requirements

### 6.1 The `build_ref` Kind System (`build_ref.mlody`)

**FR-001: `build_ref` meta-rule**

- Description: A Starlark `rule` named `build_ref` (analogous to `location` in
  `locations.mlody`). Its implementation registers a build-ref struct under kind
  `"build_ref"` in `builtins` and injects a factory function.
- Attrs accepted by the rule:

  | Attr name   | Type       | Mandatory | Default | Notes                                    |
  | ----------- | ---------- | --------- | ------- | ---------------------------------------- |
  | `name`      | `string`   | Yes       | —       | Name of the build-ref kind               |
  | `attrs`     | `dict`     | No        | None    | Attr specs for this build-ref kind       |
  | `predicate` | `callable` | No        | None    | Extra validator applied after attr check |

- Processing: register struct, inject factory — identical pattern to
  `_location_impl`.
- Priority: Must Have

**FR-002: `bazel` build-ref kind**

- Description: Represents a reference to a Bazel build target that produces a
  container image (including any command / entrypoint overrides baked into the
  image rule).
- Defined via `build_ref(name="bazel", attrs={...})` inside `build_ref.mlody`.
- Attributes:

  | Attr name | Type     | Mandatory | Description                                         |
  | --------- | -------- | --------- | --------------------------------------------------- |
  | `target`  | `string` | Yes       | Bazel label string, e.g. `"//some/path:image_name"` |

- Factory: `bazel(target="...")` injected into scope.
- `bazel()` with no arguments is invalid (target is mandatory).
- Validation: `target` must start with `"//"` or `":"`. Any other value raises
  `ValueError` at factory-call time.
- Priority: Must Have

**FR-003: `build_ref` struct shape**

- Every build-ref struct produced by `build_ref` or a factory call has at
  minimum:

  | Field  | Type          | Description                          |
  | ------ | ------------- | ------------------------------------ |
  | `kind` | `"build_ref"` | Discriminator                        |
  | `type` | `str`         | Build-ref kind name (e.g. `"bazel"`) |
  | `name` | `str`         | Same as `type` for named definitions |

- Priority: Must Have

### 6.2 The `implementation` Kind System (`implementation.mlody`)

**FR-004: `implementation` meta-rule**

- Description: A Starlark `rule` named `implementation` (analogous to
  `location`). Its implementation registers an implementation-kind struct under
  kind `"implementation"` in `builtins` and injects a factory function.
- Attrs accepted by the rule:

  | Attr name   | Type       | Mandatory | Default | Notes                                     |
  | ----------- | ---------- | --------- | ------- | ----------------------------------------- |
  | `name`      | `string`   | Yes       | —       | Name of the implementation kind           |
  | `attrs`     | `dict`     | No        | None    | Attr specs for this kind                  |
  | `predicate` | `callable` | No        | None    | Extra validator (e.g. mutual-exclusivity) |

- Priority: Must Have

**FR-005: `implementation` struct shape**

- Every implementation struct produced by an implementation factory has at
  minimum:

  | Field  | Type               | Description                                    |
  | ------ | ------------------ | ---------------------------------------------- |
  | `kind` | `"implementation"` | Discriminator                                  |
  | `type` | `str`              | Implementation kind name (`"container"`, etc.) |
  | `name` | `str`              | Same as `type` for named definitions           |

- Priority: Must Have

### 6.3 `container` Kind

**FR-006: `container` implementation kind**

- Description: Declares that an action runs inside a container image. The image
  is specified via a mandatory `build` attribute holding a build-ref struct. The
  image itself encodes the command and entrypoint; those are not repeated here.
- Defined via `implementation(name="container", attrs={...})` inside
  `implementation.mlody`.
- Attributes:

  | Attr name | Type               | Mandatory | Description                                             |
  | --------- | ------------------ | --------- | ------------------------------------------------------- |
  | `build`   | `build_ref` struct | Yes       | A build-ref kind struct, e.g. `bazel(target="//x:img")` |

- Factory: `container(build=...)` injected into scope.
- Validation: `build` must be a struct with `kind="build_ref"`.
- Priority: Must Have

### 6.4 `sandbox` Kind

**FR-006a: `sandbox` implementation kind**

- Description: Declares that an action runs inside a sandbox image. The image
  is specified via a mandatory `build` attribute holding a build-ref struct.
  Like `container`, the image itself encodes the command and entrypoint.
- Defined via `implementation(name="sandbox", attrs={...})` inside
  `implementation.mlody`.
- Attributes:

  | Attr name | Type               | Mandatory | Description                                             |
  | --------- | ------------------ | --------- | ------------------------------------------------------- |
  | `build`   | `build_ref` struct | Yes       | A build-ref kind struct, e.g. `bazel(target="//x:img")` |

- Factory: `sandbox(build=...)` injected into scope.
- Validation: `build` must be a struct with `kind="build_ref"`.
- Priority: Must Have

### 6.5 `shell_script` Kind

**FR-007: `shell_script` implementation kind**

- Description: Declares that an action runs a shell script. The script source is
  supplied either as an inline string (`content`) or as a path to a repository
  file (`file`). Exactly one of the two must be provided.
- Defined via `implementation(name="shell_script", attrs={...}, predicate=...)`
  inside `implementation.mlody`. The mutual-exclusivity constraint is enforced
  by the `predicate`.
- Attributes:

  | Attr name     | Type     | Mandatory | Description                                                                                                                              |
  | ------------- | -------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
  | `content`     | `string` | No        | Inline script text. Mutually exclusive with `file`.                                                                                      |
  | `file`        | `string` | No        | Repository-relative path to the script file. Mutually exclusive with `content`. Must not start with `/` or contain `..` path components. |
  | `interpreter` | `string` | No        | Path or name of the interpreter (e.g. `"/bin/bash"`). Optional; does not override a shebang but supplements it when provided.            |

- Mutual exclusivity: the `predicate` raises `ValueError` if both `content` and
  `file` are set, and raises `ValueError` if neither is set.
- `file` path validation: raises `ValueError` if `file` starts with `"/"` or
  contains `".."` as a path component.
- Factory: `shell_script(...)` injected into scope.
- Priority: Must Have

### 6.6 `system_binary` Kind

**FR-008: `system_binary` implementation kind**

- Description: Declares that an action invokes a binary that is already
  installed on the execution host. The binary is identified by its absolute
  path. If the binary were to come from a Bazel build, `container` with a
  `bazel(...)` build ref would be used instead.
- Defined via `implementation(name="system_binary", attrs={...}, predicate=...)`
  inside `implementation.mlody`.
- Attributes:

  | Attr name | Type     | Mandatory | Description                                        |
  | --------- | -------- | --------- | -------------------------------------------------- |
  | `path`    | `string` | Yes       | Absolute path to the binary (must start with `/`). |

- Validation: `path` must start with `"/"`. Any value not starting with `"/"`
  raises `ValueError` at factory-call time.
- Factory: `system_binary(path="...")` injected into scope.
- Priority: Must Have

### 6.6 Changes to `action.mlody`

**FR-009: Replace `implementation: string_list` with `implementation_ref`**

- Description: The `implementation` attribute of the `action` rule changes from
  type `string_list` to a new `implementation_ref` type. An `implementation_ref`
  accepts a struct with `kind="implementation"` or a string name (lazy-resolved,
  for consistency with other `_ref` types).
- The `attrs` dict in `action` is updated accordingly:

  ```starlark
  "implementation": attr(type="implementation_ref"),
  ```

- The `_validate_string_list` call in `_action_impl` is replaced by a call that
  validates the struct shape.
- The `action_struct` produced by `_action_impl` stores the implementation
  struct unchanged on `action_struct.implementation`.
- Priority: Must Have

**FR-010: `implementation_ref` type in `attrs.mlody`**

- Description: `validate_attr_value` in `attrs.mlody` must gain a branch for
  `type_ref == "implementation_ref"` that accepts:
  - A string (lazy name reference), or
  - A struct with `kind="implementation"`. This follows the existing pattern of
    `location_ref`, `action_ref`, etc.
- Priority: Must Have

**FR-011: Evaluator registration for `"implementation"` and `"build_ref"`
kinds**

- Description: `Evaluator._register` must be extended to accept
  `kind="implementation"` and `kind="build_ref"`, following the same pattern as
  `kind="location"` (FR-007 in the locations requirements).
- `builtins.lookup("implementation", name)` and
  `builtins.lookup("build_ref", name)` must return the registered structs.
- Priority: Must Have

---

## 7. Non-Functional Requirements

### 7.1 Consistency with `locations.mlody`

The code structure, naming conventions, and test pattern of
`implementation.mlody` and `build_ref.mlody` must mirror `locations.mlody` so
that a reader familiar with the location system can immediately orient in the
new modules. In particular: same `kind`/`type`/`name` field layout, same factory
injection mechanism, same `extend_attrs` usage.

### 7.2 Immutability

All implementation and build-ref structs are Starlark `Struct` instances
(immutable). No mutable state is introduced.

### 7.3 Starlark Purity

No new `python.*` calls beyond those already used in `locations.mlody` unless
explicitly approved. Every `python.*` usage must be marked with a comment for
the audit grep.

### 7.4 Inline-Only Use

Implementation and build-ref factory calls are expected to appear only inline
inside `action(...)` calls. The system does not need to support or optimise for
top-level named declarations of these objects.

---

## 8. Data Requirements

### 8.1 `build_ref` Struct Shape

| Field  | Type          | Description                         |
| ------ | ------------- | ----------------------------------- |
| `kind` | `"build_ref"` | Discriminator                       |
| `type` | `str`         | Build-ref kind name, e.g. `"bazel"` |
| `name` | `str`         | Same as `type`                      |

Per-kind additional fields for `bazel`:

| Field    | Type  | Description        |
| -------- | ----- | ------------------ |
| `target` | `str` | Bazel label string |

### 8.2 `implementation` Struct Shape

| Field  | Type               | Description                                                   |
| ------ | ------------------ | ------------------------------------------------------------- |
| `kind` | `"implementation"` | Discriminator                                                 |
| `type` | `str`              | Kind name: `"container"`, `"shell_script"`, `"system_binary"` |
| `name` | `str`              | Same as `type`                                                |

Per-kind additional fields:

**`container`:**

| Field   | Type             | Description                           |
| ------- | ---------------- | ------------------------------------- |
| `build` | build-ref struct | The build-ref for the container image |

**`shell_script`:**

| Field         | Type        | Description                                       |
| ------------- | ----------- | ------------------------------------------------- |
| `content`     | str or None | Inline script text (mutually exclusive with file) |
| `file`        | str or None | Repo-relative path to script file                 |
| `interpreter` | str or None | Interpreter path or name                          |

**`system_binary`:**

| Field  | Type | Description                 |
| ------ | ---- | --------------------------- |
| `path` | str  | Absolute path to the binary |

### 8.3 Data Retention

All structs are in-memory only; they exist for the lifetime of the evaluator
process.

---

## 9. Integration Requirements

### 9.1 Internal Module Dependencies

| Module                           | Usage                                                           |
| -------------------------------- | --------------------------------------------------------------- |
| `//mlody/core/rule.mlody`        | `rule` function — creates the meta-rules                        |
| `//mlody/common/attrs.mlody`     | `attr`, `extend_attrs`, `validate_attr_value`                   |
| `//mlody/common/action.mlody`    | Modified to load from `implementation.mlody`; attr type changed |
| `//mlody/common/build_ref.mlody` | Loaded by `implementation.mlody` for `build_ref` struct check   |

### 9.2 Evaluator Extension

`Evaluator._register` must be extended to accept `kind="implementation"` and
`kind="build_ref"` (FR-011). Factory injection via `builtins.inject` follows the
same protocol as `locations.mlody`.

---

## 10. User Interface Requirements

Not applicable — these are Starlark library modules, not UI components. The DSL
surface is documented in Section 6.

---

## 11. Reporting & Analytics Requirements

Not applicable for v1.

---

## 12. Security & Compliance Requirements

### 12.1 No Credential Storage

Implementation objects must not store credentials, secrets, or access tokens.
These are resolved out-of-band by the execution environment.

### 12.2 Absolute Path Enforcement for `system_binary`

Requiring an absolute path for `system_binary.path` prevents ambiguity about
which binary on `$PATH` would be resolved. This is a security-relevant
constraint: it makes the binary declaration explicit and host-specific.

### 12.3 Audit Trail for `python.*`

Every use of `python.*` in `implementation.mlody` and `build_ref.mlody` must be
accompanied by a comment so that `grep python\.` surfaces all non-Starlark
escape points.

---

## 13. Infrastructure & Deployment Requirements

### 13.1 BUILD File

`.mlody` files follow the same convention as `locations.mlody`: no additional
Bazel `data` entries required for the `.mlody` source files themselves.

### 13.2 Test Targets

```python
o_py_test(
    name = "implementation_test",
    srcs = ["implementation_test.py"],
    deps = [
        "//common/python/starlarkish/evaluator",
    ],
)
```

---

## 14. Testing & Quality Assurance Requirements

### 14.1 Testing Pattern

Tests must follow the `locations_test.py` / `types_test.py` pattern:

1. Read real `.mlody` sources at module import time using
   `Path(__file__).parent / "..."`.
2. Build a `_BASE_FILES` dict covering `rule.mlody`, `attrs.mlody`,
   `types.mlody`, `locations.mlody`, `build_ref.mlody`, and
   `implementation.mlody`.
3. Define a helper `_eval(extra_mlody)` that loads `implementation.mlody` and
   evaluates the extra snippet via `InMemoryFS` + `Evaluator`.
4. Each test function is standalone and self-contained.

### 14.2 Required Test Cases

| ID     | Description                                                                                              | Priority    |
| ------ | -------------------------------------------------------------------------------------------------------- | ----------- |
| TC-001 | `bazel(target="//x:y")` returns struct with `kind="build_ref"`, `type="bazel"`, `target`                 | Must Have   |
| TC-002 | `bazel()` with no arguments raises `TypeError` (target is mandatory)                                     | Must Have   |
| TC-003 | `bazel(target="not-a-label")` raises `ValueError` (does not start with `//` or `:`)                      | Must Have   |
| TC-004 | `bazel(target="//valid:target")` and `bazel(target=":local")` are both accepted                          | Must Have   |
| TC-005 | `container(build=bazel(target="//x:y"))` returns struct with `kind="implementation"`, `type="container"` | Must Have   |
| TC-006 | `container(...)` with no `build` argument raises `TypeError`                                             | Must Have   |
| TC-007 | `container(build="not-a-struct")` raises `TypeError` for invalid build attr                              | Should Have |
| TC-008 | `shell_script(content="echo hi")` returns valid struct; `file` absent                                    | Must Have   |
| TC-009 | `shell_script(file="scripts/run.sh")` returns valid struct; `content` absent                             | Must Have   |
| TC-010 | `shell_script(content="...", file="...")` raises `ValueError` (mutual exclusivity)                       | Must Have   |
| TC-011 | `shell_script()` with neither `content` nor `file` raises `ValueError`                                   | Must Have   |
| TC-012 | `shell_script(content="...", interpreter="/bin/bash")` stores interpreter correctly                      | Must Have   |
| TC-013 | `shell_script(file="/absolute/path.sh")` raises `ValueError` (absolute path rejected)                    | Must Have   |
| TC-014 | `shell_script(file="../escape.sh")` raises `ValueError` (`..` component rejected)                        | Must Have   |
| TC-015 | `system_binary(path="/usr/bin/ffmpeg")` returns struct with absolute path                                | Must Have   |
| TC-016 | `system_binary(path="relative/bin")` raises `ValueError`                                                 | Must Have   |
| TC-017 | `system_binary(path="//bazel:target")` raises `ValueError`                                               | Must Have   |
| TC-018 | `action(implementation=container(...))` stores implementation struct                                     | Must Have   |
| TC-019 | `action(implementation=["old", "string", "list"])` raises `TypeError`                                    | Must Have   |

### 14.3 Acceptance Criteria

The implementation is accepted when all TC-001 through TC-019 tests pass under
`bazel test //mlody/common:implementation_test` and the lint check
`bazel build --config=lint //mlody/common:implementation_test` is clean.

---

## 15. Training & Documentation Requirements

### 15.1 Inline Documentation

Each public factory function and the kind definitions should carry a docstring
following the style of `locations.mlody`.

### 15.2 Sandbox Table Update

The `mlody/CLAUDE.md` sandbox table should be updated to mention the injected
factory symbols (`container`, `shell_script`, `system_binary`, `bazel`) once the
modules ship.

---

## 16. Risks & Mitigation Strategies

| Risk ID | Description                                                                      | Impact | Probability | Mitigation                                                                                     | Owner        |
| ------- | -------------------------------------------------------------------------------- | ------ | ----------- | ---------------------------------------------------------------------------------------------- | ------------ |
| R-001   | Existing pipelines use `implementation: string_list`; change is breaking         | High   | Confirmed   | Provide a clear `TypeError` pointing to the new API; document migration path                   | mlody core   |
| R-002   | `build_ref` struct passed to `container.build` may not be validated early enough | Medium | Low         | Validate `kind="build_ref"` in the `container` factory predicate, not just in executor         | mlody core   |
| R-003   | Mutual-exclusivity predicate for `shell_script` is stateful (checks two attrs)   | Medium | Low         | Implement as an explicit predicate closure in `implementation.mlody`; cover with TC-008/TC-009 | mlody core   |
| R-004   | `bazel` target string syntax is not validated (only that it is a string)         | Low    | Medium      | Accepted for v1; label-syntax validation is future work                                        | Architecture |

---

## 17. Dependencies

| Dependency                                              | Type     | Status   | Impact if Delayed                                     | Owner      |
| ------------------------------------------------------- | -------- | -------- | ----------------------------------------------------- | ---------- |
| `//mlody/core/rule.mlody` — `rule`                      | Internal | Done     | None                                                  | mlody core |
| `//mlody/common/attrs.mlody`                            | Internal | Done     | None — already exists                                 | mlody core |
| `Evaluator._register("implementation", "build_ref")`    | Internal | Required | Cannot register new kind structs                      | mlody core |
| `builtins.inject` for kind factories                    | Internal | Done     | Already works per `locations.mlody`                   | mlody core |
| `validate_attr_value` branch for `"implementation_ref"` | Internal | Required | `action.mlody` cannot validate the new attribute type | mlody core |

---

## 18. Open Questions & Action Items

| ID     | Question / Action                                                                                                                  | Owner        | Target Date | Status                                                                                                                        |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------ | ----------- | ----------------------------------------------------------------------------------------------------------------------------- |
| OQ-001 | Should `bazel` target strings be validated for label syntax (e.g. must start with `//` or `:`)?                                    | Architecture | 2026-04-15  | Closed — yes, validate that the string starts with `//` or `:` at factory-call time                                           |
| OQ-002 | Should `shell_script.file` paths be validated to be in-tree (repo-relative) vs. any string?                                        | Architecture | 2026-04-15  | Closed — yes, validate that the path does not start with `/` (ruling out absolute paths) and does not contain `..` components |
| OQ-003 | Will additional build-ref kinds (e.g. OCI image by digest, Docker Hub reference) follow the same `build_ref` meta-rule pattern?    | Architecture | —           | Deferred                                                                                                                      |
| OQ-004 | Should `container.args` and `system_binary.args` support structured argument types (e.g. key-value pairs) rather than raw strings? | Architecture | —           | Deferred                                                                                                                      |
| OQ-005 | How does the executor distinguish "no args" (`args=None`) from "empty args list" (`args=[]`)?                                      | mlody core   | 2026-04-15  | Closed — no semantic difference; both mean "no extra arguments"                                                               |

---

## 19. Revision History

| Version | Date       | Author                  | Changes       |
| ------- | ---------- | ----------------------- | ------------- |
| 1.0     | 2026-04-15 | Requirements Analyst AI | Initial draft |

---

## Appendices

### Appendix A: Glossary

| Term                 | Definition                                                                                                           |
| -------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Implementation kind  | One of `container`, `shell_script`, `system_binary` — describes _how_ an action is executed.                         |
| Build ref            | A structured reference to a build-system artifact (e.g. a Bazel target) used to produce a container image.           |
| `bazel` build-ref    | A `build_ref` kind whose `target` field holds a Bazel label string.                                                  |
| Inline declaration   | A factory call appearing directly as an attribute value inside `action(...)`, not at top-level scope.                |
| Mutual exclusivity   | A constraint where exactly one of two attributes (`content` / `file` for `shell_script`) must be provided, not both. |
| `implementation_ref` | A new attr type accepted by `validate_attr_value`, analogous to `location_ref` and `action_ref`.                     |
| `string_list`        | The previous type of `action.implementation` — a flat list of command tokens. Replaced by this work.                 |

### Appendix B: References

- `mlody/common/action.mlody` — current `action` rule implementation; to be
  modified per FR-009
- `mlody/common/locations.mlody` — primary design reference for the kind-system
  pattern
- `mlody/common/attrs.mlody` — shared attribute infrastructure
- `mlody/common/types_test.py`, `mlody/common/locations_test.py` — test pattern
  references
- `mlody/CLAUDE.md` — framework conventions, Starlark vs. Python rules
- `mlody/core/rule.mlody` — `rule` function

### Appendix C: Illustrative DSL Usage

```starlark
load("//mlody/common/action.mlody", "action")
load("//mlody/common/locations.mlody")

# Container action: image produced by a Bazel target
action(
    name = "train",
    inputs  = [training_data],
    outputs = [model_checkpoint],
    implementation = container(
        build = bazel(target = "//mlody/teams/lexica/images:trainer"),
    ),
)

# Shell script action: inline script
action(
    name = "preprocess",
    inputs  = [raw_data],
    outputs = [clean_data],
    implementation = shell_script(
        content     = "#!/bin/bash\nset -euo pipefail\npython normalize.py $@",
        interpreter = "/bin/bash",
    ),
)

# Shell script action: file reference
action(
    name = "package",
    inputs  = [artifacts],
    outputs = [bundle],
    implementation = shell_script(
        file = "tools/package.sh",
    ),
)

# System binary action
action(
    name = "convert",
    inputs  = [video_source],
    outputs = [video_mp4],
    implementation = system_binary(
        path = "/usr/bin/ffmpeg",
    ),
)
```

### Appendix D: Struct Shape Reference

```
build_ref struct (bazel):       implementation struct (container):
  kind   = "build_ref"            kind   = "implementation"
  type   = "bazel"                type   = "container"
  name   = "bazel"                name   = "container"
  target = "//some:image"         build  = <build_ref struct>

implementation struct (shell_script):   implementation struct (system_binary):
  kind         = "implementation"         kind = "implementation"
  type         = "shell_script"           type = "system_binary"
  name         = "shell_script"           name = "system_binary"
  content      = "#!/bin/bash\n..." | None  path = "/usr/bin/tool"
  file         = "scripts/run.sh" | None
  interpreter  = "/bin/bash" | None
```

---

**End of Requirements Document**
