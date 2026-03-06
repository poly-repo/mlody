# Requirements Document: mlody/common/locations.mlody

**Version:** 1.2 **Date:** 2026-03-05 **Prepared by:** Requirements Analyst AI
**Status:** Draft

---

## 1. Executive Summary

Mlody pipelines need a principled way to describe _where_ a value lives — its
physical or logical address in a storage system. Currently the framework has a
rich type system (`types.mlody`) that characterises the _shape_ of a value, but
no corresponding system for characterising its _location_. Without this,
producer/consumer compatibility cannot be checked at pipeline-configuration
time; mismatches are discovered only at runtime.

This document specifies `mlody/common/locations.mlody`, a new Starlark module
that introduces the `location` rule and a set of predefined location kinds
(`s3`, `posix`). Locations are purely declarative: they are references/pointers
that carry zero execution semantics. They are one orthogonal facet of a richer
"value descriptor" concept and participate in an extended unification model for
producer/consumer consistency checking.

Success is defined as: a spec writer can design `locations.mlody` without
returning to stakeholders for clarification on any of the topics covered below,
and the resulting implementation passes a test suite structured identically to
`types_test.py`.

---

## 2. Project Scope

### 2.1 In Scope

- A new shared Starlark library `mlody/common/attrs.mlody` that houses the
  attr-definition and struct-extension utilities (`extend_attrs`, `attr`,
  `field`, `_make_factory`, `_validate_attr_value`) currently embedded in
  `types.mlody`
- Refactoring `types.mlody` to `load("//mlody/common/attrs.mlody", ...)` rather
  than defining those utilities inline
- The `location` rule (Starlark `rule`, mirrors `typedef` in `types.mlody`)
- Two predefined location kinds: `s3` and `posix`
- Factory function injection for each predefined kind (`s3()`, `posix()`)
- User-defined location extension via the `location` rule (teams can create
  narrowed sub-kinds)
- Attribute inheritance and narrowing via the factory call pattern (same
  mechanism as `integer(min=0, max=150)` in `types.mlody`)
- Registration in `builtins` under a new `"location"` kind
- A `validator` attached to each location struct, callable for consistency
  checking
- Two evaluator changes: `Evaluator._register` accepting `kind="location"` and
  `builtins.inject` delivering location factories into the sandbox

### 2.2 Out of Scope (v1)

- Execution semantics (fetching, cloning, pulling — locations are inert
  references)
- Credentials and access-control lists attached to a location declaration
- Multiple candidate input locations per value (single location per value
  descriptor only; multi-location is future work)
- Git repository location kind
- Container image registry location kind
- SSH/HTTP access to remote POSIX file systems
- An abstract common base type (`storage_location` or similar) unifying `s3` and
  `posix`
- The full unification / conflict-resolver API (shape defined here as an open
  question; the consistency check extension point is noted but not specified)
- Preference-ordered fallback locations

### 2.3 Assumptions

- `locations.mlody` will `load("//mlody/common/attrs.mlody", ...)` to access
  `extend_attrs`, `attr`, `field`, and `_make_factory`. This is a firm
  architectural decision (see FR-000).
- All location attribute values are optional at declaration time: a bare `s3()`
  or `posix()` is valid and means "any location of this kind."
- The `python.*` escape namespace is available for the same operations used in
  `types.mlody` (`python.getattr`, `python.hasattr`, `python.re`, etc.).
- Starlark `None` equality uses `==`/`!=` (not `is`/`is not`), consistent with
  the rest of the codebase.

### 2.4 Constraints

- The file must be valid Starlark (eventual pure-Starlark goal); new `python.*`
  usage requires explicit approval before being added.
- No raw Bazel `py_*` rules; use `o_py_library`, `o_py_test`.
- `locations.mlody` must appear in the `data = [...]` list of any `o_py_library`
  that loads it.
- Attribute redeclaration in child `location` definitions follows the same
  strict no-redeclaration rule as `extend_attrs`: a child may not redeclare an
  attr already owned by a parent. Narrowing is achieved by passing constraining
  values through the factory, not by redefining the attr spec.

---

## 3. Stakeholders

| Role               | Group                    | Responsibilities                                         |
| ------------------ | ------------------------ | -------------------------------------------------------- |
| Framework author   | mlody core team          | Implements `locations.mlody`; owns the `location` rule   |
| Pipeline authors   | Team/project DSL authors | Define value descriptors using `s3()`, `posix()`, etc.   |
| Platform operators | Infra/DevEx              | May define team-scoped narrowed location kinds           |
| Spec writer        | Architecture             | Consumes this document to produce `locations.mlody` spec |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Enable producer/consumer location compatibility to be checked at
  pipeline-configuration time, not at runtime.
- **BR-002:** Provide a consistent, extensible DSL pattern for declaring where
  values live, aligned with the existing `typedef`/factory pattern teams already
  know.
- **BR-003:** Allow platform teams to publish narrowed, project-specific
  location kinds without modifying framework source.

### 4.2 Success Metrics

- **KPI-001:** All predefined locations (`s3`, `posix`) are exercisable via
  their factory functions in `.mlody` files after the two confirmed evaluator
  changes (FR-007: `_register` extension; FR-009: factory injection).
- **KPI-002:** A user-defined location (e.g. `my_team_s3`) that narrows `bucket`
  to a specific value rejects values with a different bucket at validation time.
- **KPI-003:** Test coverage follows the `types_test.py` pattern; all tests pass
  under `bazel test //mlody/common:locations_test`.

---

## 5. User Requirements

### 5.1 User Personas

**Persona 1: Pipeline Author**

- Declares inputs/outputs for pipeline tasks.
- Needs: simple, terse factory calls (`s3()`, `posix(path="/data")`).
- Pain point today: no way to declare where a value lives; mismatches surface at
  runtime.

**Persona 2: Platform Operator**

- Defines team-scoped location conventions (e.g., "all outputs go to bucket
  `team-prod`, prefix `runs/`").
- Needs: ability to define a named sub-kind of `s3` with pre-filled attrs and/or
  a predicate, registered under a team name.
- Uses the `location` rule in a team-level `.mlody` file.

### 5.2 User Stories

**Epic 1: Predefined location factories**

- **US-001:** As a pipeline author, I want to write `s3()` to declare "any S3
  location" so that I can express a loose producer/consumer contract.
  - Acceptance Criteria: `s3()` evaluates to a location struct with
    `kind="location"`, `type="s3"`, no required attributes, and a validator that
    accepts any value.
  - Priority: Must Have

- **US-002:** As a pipeline author, I want to write
  `s3(bucket="my-bucket", region="us-east-1")` to narrow an S3 location so that
  I can enforce bucket and region constraints.
  - Acceptance Criteria: The returned struct's validator rejects values that do
    not match the declared bucket/region.
  - Priority: Must Have

- **US-003:** As a pipeline author, I want to write `posix(path="/data/runs")`
  to declare a POSIX path location.
  - Acceptance Criteria: The returned struct's validator accepts the declared
    path and rejects other paths.
  - Priority: Must Have

**Epic 2: User-defined location kinds**

- **US-004:** As a platform operator, I want to use the `location` rule to
  define `my_team_s3` as a child of `s3` with a mandatory bucket predicate, so
  that all team pipelines are checked against team storage conventions.
  - Acceptance Criteria:
    `location(name="my_team_s3", base=s3(bucket="team-prod"))` registers
    `my_team_s3` and injects a `my_team_s3()` factory.
  - Priority: Must Have

- **US-005:** As a platform operator, I want child location definitions to
  inherit all parent attributes without redeclaring them, so that the
  inheritance chain is unambiguous.
  - Acceptance Criteria: Attempting to redeclare an inherited attr raises a
    `ValueError` at definition time (same behaviour as `extend_attrs` in
    `attrs.mlody`).
  - Priority: Must Have

---

## 6. Functional Requirements

### 6.0 Shared Attribute Infrastructure (`attrs.mlody`)

**FR-000: Create `mlody/common/attrs.mlody` as a shared Starlark library**

- Description: The attr-definition and struct-extension utilities currently
  embedded in `types.mlody` must be extracted into a new Starlark file,
  `mlody/common/attrs.mlody`. Both `types.mlody` and `locations.mlody` will
  `load("//mlody/common/attrs.mlody", ...)` to import these symbols. No logic is
  duplicated across modules.
- Rationale: `locations.mlody` requires the same machinery as `types.mlody`.
  Duplicating it would create a maintenance hazard; re-exporting through
  `types.mlody` would introduce an inappropriate coupling between peer modules.
- File type: `.mlody` (Starlark), not a Python module. `.mlody` files are not
  currently bundled as Bazel `data` dependencies in any target — this is the
  deliberate current state of the project and must not be changed by this work.
- Symbols exported by `attrs.mlody`:

  | Symbol                 | Description                                                                         |
  | ---------------------- | ----------------------------------------------------------------------------------- |
  | `attr`                 | Declares a typed, optionally mandatory, optionally defaulted attribute spec         |
  | `field`                | Declares a typed field for use in `map()` or `typedef(fields=[...])`                |
  | `extend_attrs`         | Creates a new struct inheriting from a base, merging attrs and composing validators |
  | `_make_factory`        | Returns a factory callable for a registered type/location struct                    |
  | `_validate_attr_value` | Validates a single attr value against its declared type spec                        |

- Rename: `extend_type` (current name in `types.mlody`) becomes `extend_attrs`
  in `attrs.mlody`. The new name reflects that the function is not type-specific
  — it is a general struct-definition extension utility shared by types and
  locations. All call sites in `types.mlody` must be updated accordingly.
- `types.mlody` refactoring: remove the inline definitions of all five symbols
  above and replace them with `load("//mlody/common/attrs.mlody", ...)`. No
  behavioural change to the type system is permitted.
- Priority: Must Have — this must be completed before `locations.mlody` can be
  implemented.

### 6.1 The `location` Rule

**FR-001: `location` rule definition**

- Description: A Starlark `rule` (via `//mlody/core/rule.mlody`) named
  `location`, parallel to `typedef`. Its implementation function
  (`_location_impl`) registers a location struct under kind `"location"` and
  injects a factory function into the evaluator scope.
- Attrs accepted by the rule:

  | Attr name     | Type       | Mandatory | Default | Notes                                     |
  | ------------- | ---------- | --------- | ------- | ----------------------------------------- |
  | `name`        | `string`   | Yes       | —       | Name of the location kind                 |
  | `description` | `string`   | No        | None    | Human-readable description                |
  | `base`        | `location` | No        | None    | Parent location struct (for inheritance)  |
  | `attrs`       | `dict`     | No        | None    | Additional attr specs (same as `typedef`) |
  | `predicate`   | `callable` | No        | None    | Extra validator applied after base chain  |
  | `abstract`    | `bool`     | No        | None    | If True, validator is a no-op             |

- Processing: identical to `_type_impl` in `types.mlody`, substituting kind
  `"location"` for `"type"` throughout.
- Priority: Must Have

**FR-002: Factory injection**

- Description: After registering a location struct, `location` injects a factory
  function under the location's name (e.g., `s3`, `posix`) into the evaluator
  scope via `builtins.inject`.
- The factory is produced by `_make_factory` (loaded from `attrs.mlody`).
- The factory accepts only keyword arguments matching the location's
  `_allowed_attrs` chain. Passing an unknown kwarg raises `TypeError`.
- Calling the factory with no arguments returns the base location struct
  unchanged.
- Priority: Must Have

**FR-003: Attribute inheritance and narrowing**

- Description: A child `location` definition inherits all `_allowed_attrs` from
  its `base`. It may declare additional attrs (not conflicting with inherited
  ones). Narrowing is expressed by passing constraining values to the factory,
  not by redeclaring attrs.
- Conflict rule: if a child's `attrs` dict contains a key that the parent
  already declares, `extend_attrs` raises `ValueError` at definition time.
- Priority: Must Have

**FR-004: Validator composition**

- Description: Each location struct carries a `validator` field (a callable
  `value -> True | raises`). Validators compose along the inheritance chain via
  `extend_attrs`'s `composed_validator` logic. The predicate (if given) is
  applied after the base validator chain.
- Because location kinds are not primitive kinds (`"integer"`, `"string"`,
  etc.), `extend_attrs`'s non-primitive branch applies: the base validator is
  delegated to, then the extra validator runs.
- Priority: Must Have

### 6.2 Predefined Location Kinds

**FR-005: `s3` location**

- Description: Represents an object in or within an AWS S3-compatible store.
- All attributes are optional (bare `s3()` means "any S3 location").
- Attributes:

  | Attr name | Type     | Mandatory | Description                              |
  | --------- | -------- | --------- | ---------------------------------------- |
  | `bucket`  | `string` | No        | S3 bucket name                           |
  | `prefix`  | `string` | No        | Key prefix (path within the bucket)      |
  | `region`  | `string` | No        | AWS region identifier (e.g. `us-east-1`) |

- Factory: `s3()` injected into scope.
- Priority: Must Have

**FR-006: `posix` location**

- Description: Represents a path in a POSIX-compatible file system accessible to
  the execution environment.
- All attributes are optional (bare `posix()` means "any POSIX path").
- Attributes:

  | Attr name | Type     | Mandatory | Description               |
  | --------- | -------- | --------- | ------------------------- |
  | `path`    | `string` | No        | Absolute or relative path |

- Factory: `posix()` injected into scope.
- Priority: Must Have

### 6.3 Registration and Evaluator Extension

**FR-007: Extend `Evaluator._register` to accept `kind="location"`**

- Description: `Evaluator._register` in
  `common/python/starlarkish/evaluator/evaluator.py` must be extended to handle
  `kind="location"` in addition to the existing `kind="type"`. This allows
  `builtins.register("location", struct)` inside `.mlody` files to store
  location structs on the evaluator instance, where they can be looked up by
  `builtins.lookup("location", name)`.
- Implementation note: the change is expected to be a single-line addition
  mirroring the existing `"type"` dispatch in `_register`. The mlody CLAUDE.md
  explicitly identifies `Evaluator._register` as the correct extension point
  ("Extend `Evaluator._register` in `evaluator.py` to add new kinds").
- Priority: Must Have

**FR-008: `builtins.lookup` for location kind**

- Description: `builtins.lookup("location", name)` must return the registered
  location struct by name, enabling cross-file references (e.g., a team file
  looking up `s3` to use as a base).
- Priority: Must Have

**FR-009: Inject location factory functions into the Starlark sandbox**

- Description: After registering a location struct, `_location_impl` must call
  `builtins.inject(name, factory)` to make the factory callable available as a
  top-level symbol inside any `.mlody` file that loads `locations.mlody`. This
  is identical to the mechanism `typedef` uses for type factories (e.g.,
  `integer()`, `string()`). Without this step, `s3(...)`, `posix(...)`, and
  user-defined location factories would not be callable in Starlark.
- Priority: Must Have

---

## 7. Non-Functional Requirements

### 7.1 Consistency with `types.mlody`

- The code structure, naming conventions, docstring style, and test pattern of
  `locations.mlody` must mirror `types.mlody` so that a reader familiar with the
  type system can immediately orient in the location module.

### 7.2 Immutability

- All location structs are Starlark `Struct` instances (immutable). No mutable
  state is introduced.

### 7.3 Serializability

- Location struct attributes must be plain Starlark scalars (`str`, `int`,
  `bool`) or `None`. No Python-only objects are stored in attrs, ensuring
  structs can be serialised and introspected by the host.

### 7.4 Cross-environment portability

- Attribute values are strings/scalars only; no host-OS-specific objects. A
  `posix(path="/data")` declaration is meaningful regardless of where the
  evaluator runs.

### 7.5 Starlark purity

- No new `python.*` calls beyond those already present in `types.mlody` unless
  explicitly approved. Every `python.*` usage must be marked with a comment for
  the audit grep.

---

## 8. Data Requirements

### 8.1 Location Struct Shape

Every location struct produced by `location` or a factory call has at minimum:

| Field            | Type         | Description                                              |
| ---------------- | ------------ | -------------------------------------------------------- |
| `kind`           | `"location"` | Discriminator (analogous to `"type"` in type structs)    |
| `type`           | `str`        | Name of the location kind (e.g., `"s3"`, `"posix"`)      |
| `name`           | `str`        | Same as `type` for named definitions                     |
| `attributes`     | `dict`       | Current constrained attribute values                     |
| `_allowed_attrs` | `dict`       | Attr name → type-spec, for factory validation            |
| `validator`      | `callable`   | Composed validator for consistency checking              |
| `abstract`       | `bool`       | If True, validator is a no-op                            |
| `_root_kind`     | `str`        | Root kind name, propagated through the inheritance chain |

Optional fields (present only when set):

| Field         | Type       | Description                   |
| ------------- | ---------- | ----------------------------- |
| `description` | `str`      | Human-readable description    |
| `predicate`   | `callable` | User-supplied extra validator |

### 8.2 Data Retention

Location structs are in-memory only; they exist for the lifetime of the
evaluator process.

---

## 9. Integration Requirements

### 9.1 Internal Module Dependencies

| Module                       | Usage                                                                    |
| ---------------------------- | ------------------------------------------------------------------------ |
| `//mlody/core/rule.mlody`    | `rule` function — creates the `location` rule                            |
| `//mlody/common/attrs.mlody` | `extend_attrs`, `attr`, `field`, `_make_factory`, `_validate_attr_value` |

Note: `attrs.mlody` does not yet exist; its creation is a prerequisite
requirement (FR-000). `types.mlody` will be refactored to load from it at the
same time.

### 9.2 Evaluator Extension

`Evaluator._register` in `common/python/starlarkish/evaluator/evaluator.py` must
be extended to accept `kind="location"` (FR-007), and `builtins.inject` must
deliver location factory functions into the sandbox (FR-009). Both are firm
requirements confirmed by stakeholders. The mlody CLAUDE.md explicitly names
`Evaluator._register` as the correct extension point for new kinds.

---

## 10. User Interface Requirements

Not applicable — `locations.mlody` is a Starlark library module, not a UI
component. The "interface" is the DSL surface documented in Section 6.

---

## 11. Reporting & Analytics Requirements

Not applicable for v1.

---

## 12. Security & Compliance Requirements

### 12.1 No Credential Storage

Location declarations must not store credentials, secrets, or access keys.
Credentials are resolved out-of-band by the execution environment.

### 12.2 Audit Trail for `python.*`

Every use of `python.*` in `locations.mlody` must be accompanied by a comment so
that `grep python\.` surfaces all non-Starlark escape points for audit.

---

## 13. Infrastructure & Deployment Requirements

### 13.1 BUILD File

`.mlody` files are not currently bundled as Bazel `data` dependencies — this is
the deliberate current state of the project. `attrs.mlody`, `locations.mlody`,
and the refactored `types.mlody` follow the same convention: no BUILD `data`
entries are required or added for these files.

### 13.2 Test Target

The test file `locations_test.py` reads `.mlody` sources from the real
filesystem at import time (before any pyfakefs fixture activates), following the
pattern in `types_test.py`. The Bazel test target is:

```python
o_py_test(
    name = "locations_test",
    srcs = ["locations_test.py"],
    deps = [
        "//common/python/starlarkish/evaluator",
    ],
)
```

---

## 14. Testing & Quality Assurance Requirements

### 14.1 Testing Pattern

Tests must follow the `types_test.py` pattern exactly:

1. Read real `.mlody` sources at module import time (before any pyfakefs fixture
   activates) using `Path(__file__).parent / "..."`.
2. Build an `_BASE_FILES` dict covering `rule.mlody`, `attrs.mlody`,
   `types.mlody`, and `locations.mlody`.
3. Define a helper `_eval(extra_mlody)` that loads `locations.mlody` and
   evaluates the extra snippet via `InMemoryFS` + `Evaluator`.
4. Each test function is standalone and self-contained.

### 14.2 Required Test Cases

| ID     | Description                                                                                       | Priority    |
| ------ | ------------------------------------------------------------------------------------------------- | ----------- |
| TC-001 | `s3()` returns a struct with `kind="location"`, `type="s3"`                                       | Must Have   |
| TC-002 | `s3(bucket="b")` validator accepts `"b"` and rejects `"other"`                                    | Must Have   |
| TC-003 | `s3(region="us-east-1")` validator enforces region                                                | Must Have   |
| TC-004 | `posix()` returns a struct with `kind="location"`, `type="posix"`                                 | Must Have   |
| TC-005 | `posix(path="/data")` validator accepts `/data` and rejects `/other`                              | Must Have   |
| TC-006 | User-defined `location(name="team_s3", base=s3(bucket="prod"))` registers and injects `team_s3()` | Must Have   |
| TC-007 | Child location inherits parent attrs without redeclaration                                        | Must Have   |
| TC-008 | Child location with `attrs` conflicting with parent raises `ValueError`                           | Must Have   |
| TC-009 | `location(name="any_s3", base=s3())` with `predicate=` enforces predicate                         | Should Have |
| TC-010 | Bare `s3()` validator passes any value (no constraints)                                           | Must Have   |
| TC-011 | Bare `posix()` validator passes any value (no constraints)                                        | Must Have   |
| TC-012 | Unknown kwarg to factory raises `TypeError`                                                       | Must Have   |
| TC-013 | `types.mlody` continues to pass all existing tests after refactoring to load from `attrs.mlody`   | Must Have   |

### 14.3 Acceptance Criteria

The `locations.mlody` implementation is accepted when all TC-001 through TC-012
tests pass under `bazel test //mlody/common:locations_test` and the lint check
`bazel build --config=lint //mlody/common:locations_test` is clean.

---

## 15. Training & Documentation Requirements

### 15.1 Inline Documentation

Each public function and the `location` rule definition should carry a docstring
following the style of `types.mlody`.

### 15.2 Sandbox Table Update

The `mlody/CLAUDE.md` sandbox table should be updated to mention `location`
alongside `typedef` once the module ships.

---

## 16. Risks & Mitigation Strategies

| Risk ID | Description                                                      | Impact | Probability | Mitigation                                                        | Owner        |
| ------- | ---------------------------------------------------------------- | ------ | ----------- | ----------------------------------------------------------------- | ------------ |
| R-001   | `builtins.register("location", ...)` not supported by evaluator  | High   | Resolved    | Confirmed requirement: extend `Evaluator._register` (FR-007)      | mlody core   |
| R-002   | `_make_factory` / `extend_attrs` are private; can't be loaded    | Medium | Resolved    | Confirmed requirement: extract to `attrs.mlody` (FR-000)          | mlody core   |
| R-003   | Consistency/unification API not specified; blocked for checking  | Medium | High        | Scope v1 to struct shape and validator only; unification is v2    | Architecture |
| R-004   | Location struct shape diverges from type struct; harder to unify | Low    | Low         | Mirror field names exactly; use same `kind`/`type`/`name` pattern | Spec writer  |

---

## 17. Dependencies

| Dependency                                        | Type     | Status   | Impact if Delayed                       | Owner        |
| ------------------------------------------------- | -------- | -------- | --------------------------------------- | ------------ |
| `//mlody/core/rule.mlody` — `rule`                | Internal | Done     | None                                    | mlody core   |
| `//mlody/common/attrs.mlody` — FR-000             | Internal | Required | Cannot implement `locations.mlody`      | mlody core   |
| `types.mlody` refactor to load `attrs.mlody`      | Internal | Required | Test suite regression if skipped        | mlody core   |
| `Evaluator._register("location")` — FR-007        | Internal | Required | Cannot register location structs        | mlody core   |
| `builtins.inject` for location factories — FR-009 | Internal | Required | Factories unavailable in sandbox        | mlody core   |
| Value descriptor concept (broader)                | Future   | Design   | Locations will be standalone until then | Architecture |

---

## 18. Open Questions & Action Items

| ID     | Question / Action                                                                                                                                                                           | Owner        | Target Date | Status                                                                                                                                 |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| OQ-001 | Should `extend_type`, `_make_factory`, `_validate_attr_value` be exported from `types.mlody`, or should `locations.mlody` use a different sharing strategy?                                 | mlody core   | 2026-03-05  | Closed — extract to `attrs.mlody` (Starlark file); rename `extend_type` to `extend_attrs`. See FR-000.                                 |
| OQ-002 | Does `Evaluator._register` already support arbitrary kind strings, or does it require a code change to accept `"location"`? And must factories be injected into the sandbox?                | mlody core   | 2026-03-05  | Closed — two firm requirements: extend `_register` for `kind="location"` (FR-007) and inject factories via `builtins.inject` (FR-009). |
| OQ-003 | What is the concrete API for the unification / conflict-resolver that will consume location structs during consistency checking?                                                            | Architecture | —           | Deferred to v2                                                                                                                         |
| OQ-004 | Should an abstract `storage_location` base be introduced (even with no shared attrs) for type-checking "this field expects any storage location"?                                           | Architecture | —           | Deferred                                                                                                                               |
| OQ-005 | Credentials / ACL: when this is revisited, should they be a separate facet of the value descriptor or an optional field on the location struct?                                             | Architecture | —           | Deferred                                                                                                                               |
| OQ-006 | Multiple input locations per value descriptor: what is the unification semantics when a consumer declares `[s3(bucket="a"), posix()]` and a producer declares `s3(bucket="a", prefix="x")`? | Architecture | —           | Deferred to v2                                                                                                                         |

---

## 19. Revision History

| Version | Date       | Author                  | Changes                                                                                                    |
| ------- | ---------- | ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-03-05 | Requirements Analyst AI | Initial draft                                                                                              |
| 1.1     | 2026-03-05 | Requirements Analyst AI | Incorporated partial OQ-001/OQ-002 resolutions from prior session (incomplete — superseded by 1.2)         |
| 1.2     | 2026-03-05 | Requirements Analyst AI | Close OQ-001 (attrs.mlody, extend_attrs rename, FR-000); close OQ-002 (FR-007, FR-009); retire R-001/R-002 |

---

## Appendices

### Appendix A: Glossary

| Term             | Definition                                                                                                             |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Location         | A declarative reference to where a value physically or logically resides.                                              |
| Location kind    | A named class of location (e.g., `s3`, `posix`). Analogous to a type in `types.mlody`.                                 |
| Location struct  | The Starlark `Struct` instance produced by a `location` rule or factory call.                                          |
| Factory function | The injected callable (e.g., `s3()`) that creates configured location structs.                                         |
| Narrowing        | Passing constraining attribute values to a factory to produce a more specific location.                                |
| Unification      | The consistency-checking algorithm that determines whether a producer location is compatible with a consumer location. |
| Value descriptor | The richer per-value concept of which location is one orthogonal facet (type, location, physical representation, …).   |
| `typedef`        | The analogous rule in `types.mlody` from which `location` borrows its design.                                          |
| `extend_attrs`   | The function in `attrs.mlody` (renamed from `extend_type`) that creates an inheriting struct with composed validators. |

### Appendix B: References

- `mlody/common/types.mlody` — type system implementation (primary design
  reference; to be refactored to load from `attrs.mlody`)
- `mlody/common/attrs.mlody` — new shared Starlark library (to be created per
  FR-000)
- `mlody/common/types_test.py` — test pattern reference
- `mlody/CLAUDE.md` — framework conventions, Starlark vs. Python rules, sandbox
  table
- `mlody/core/rule.mlody` — `rule` function used by both `typedef` and
  `location`
- `common/python/starlarkish/evaluator/evaluator.py` — `Evaluator._register`
  extension point (FR-007)

### Appendix C: Illustrative DSL Usage

```starlark
load("//mlody/common/locations.mlody")

# Any S3 location
out_loc = s3()

# Narrowed S3 location
prod_loc = s3(bucket="team-prod", region="us-east-1", prefix="runs/")

# Any POSIX path
scratch = posix()

# Specific POSIX path
data_dir = posix(path="/mnt/data/runs")

# User-defined team location (in a team .mlody file)
location(
    name = "team_s3",
    base = s3(bucket="team-prod"),
    description = "All team outputs go to the team-prod bucket.",
)

# team_s3() is now available — further narrows to a prefix
model_output = team_s3(prefix="models/")
```

### Appendix D: Struct Shape Comparison

```
Type struct (types.mlody):        Location struct (locations.mlody):
  kind         = "type"             kind         = "location"
  type         = "integer"          type         = "s3"
  name         = "integer"          name         = "s3"
  attributes   = {min: 0, ...}      attributes   = {bucket: "x", ...}
  _allowed_attrs = {...}            _allowed_attrs = {...}
  validator    = <fn>               validator    = <fn>
  abstract     = False              abstract     = False
  _root_kind   = "integer"          _root_kind   = "s3"
```

---

**End of Requirements Document**
