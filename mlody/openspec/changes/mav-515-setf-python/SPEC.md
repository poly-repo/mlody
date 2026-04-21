# SPEC: mlody Generic Writable Path Selection (`setf`)

**Version:** 1.0 **Date:** 2026-04-20 **Architect:** @vitruvius **Status:**
Draft **Requirements:** `mlody/docs/wip/setf-requirements.md`

---

## Executive Summary

This change adds a Python-first `setf` engine to mlody. The engine reuses the
existing traversal grammar to resolve one or more writable places, validates the
entire operation before any mutation, commits in either `inplace` or `copy`
mode, and appends lineage to every touched place. It is explicitly designed to
be backend-agnostic: the initial implementation targets in-memory mlody/Starlark
values, while future changes can plug in SQL-based selection, parquet-backed
writes, or permission-aware external stores without changing the top-level API.

The most important design choice is that assignment targets are *places*, not
just current values. A place may represent either:

1. a direct writable slot, such as `.config.learning_rate` or `[3]`, or
2. a projected region, such as `[::2]`, whose lineage should stay attached to
   the owning aggregate value rather than exploding into per-element old/new
   snapshots.

Bulk assignment is transaction-like: the engine resolves all target places,
checks that they are writable, checks that they share the same write contract
(exact type and exact representation in v1), validates the new value once
against that contract, and only then commits.

This spec intentionally excludes `.mlody` surface syntax. That should be a
follow-up change once the Python engine, place model, and lineage semantics are
stable.

**Requirements addressed:** FR-SF-001 through FR-SF-017, NFR-SF-MAIN-001/002,
NFR-SF-REL-001/002, NFR-SF-COMP-001.

---

## Architecture Overview

```
mlody/core/place.py            (NEW)
  AssignmentMode               "inplace" | "copy"
  Place                        direct or projected writable region
  PlaceSet                     tuple[Place, ...] + uniformity helpers
  SetterStrategy               Protocol for preflight + commit

mlody/core/lineage.py          (NEW)
  build_lineage_event(...)
  append_lineage(...)

mlody/core/setf_strategies.py  (NEW)
  StructFieldSetter
  ListIndexSetter
  DictKeySetter
  SequenceSliceSetter
  Expansion helpers for wildcard / recursive descent

mlody/core/setf.py             (NEW, public API)
  resolve_places(root, selector)
  can_setf(root, selector, new_value)
  setf(root, selector, new_value, *, mode, author, reason, timestamp)

mlody/core/virtual_value.py    (MODIFIED)
  Writable hook for virtual values / place resolution helpers

mlody/resolver/label_value.py  (MODIFIED)
  Shared selector normalization helpers reused by write side
```

High-level flow:

```
root + selector + new_value
  --> parse / normalise selector to PathExpression
  --> resolve_places(...) -> PlaceSet
  --> preflight:
        every place writable?
        same declared type?
        same declared representation?
        new_value validates?
  --> commit all places in requested mode
  --> append lineage event per touched place
  --> return updated root
```

Key distinction between direct and projected places:

```
foo.bar           -> one direct Place
foo[3]            -> one direct Place
foo[::2]          -> one projected Place
foo.outputs[*]    -> many direct Places (wildcard expands)
foo..sha          -> many direct Places (recursive descent expands)
```

For a projected place such as `foo[::2]`, the write contract is the element
contract, but the lineage sink is the owning aggregate `foo` with accessor
`[::2]`.

---

## Technical Stack

| Concern | Choice |
|---------|--------|
| Language | Python 3.13.2 |
| Type checking | basedpyright strict |
| Formatting / linting | ruff |
| Bazel rules | `o_py_library`, `o_py_test` |
| Test framework | pytest via `o_py_test` |
| Third-party deps | None — stdlib + existing mlody/starlarkish code only |

No new runtime dependencies are required in v1.

---

## Detailed Component Specifications

### 1. `mlody/core/place.py`

**Purpose:** Define the backend-neutral place model consumed by the assignment
engine.

**Public types:**

```python
AssignmentMode = Literal["inplace", "copy"]

class SetterStrategy(Protocol):
    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None: ...
    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object: ...

@dataclass(frozen=True)
class Place:
    root: object
    owner: object
    selector: PathExpression
    accessor: str
    current_value: object
    declared_type: object | None
    declared_representation: object | None
    strategy: SetterStrategy
    projected: bool = False
    lineage_sink: object | None = None

@dataclass(frozen=True)
class PlaceSet:
    places: tuple[Place, ...]
```

**Semantics:**

- `owner` is the value/container to which the strategy writes.
- `accessor` is the canonical serialized selector fragment for this place.
  - For direct places, this is the direct path fragment, such as `.foo` or
    `[3]`.
  - For projected places, it preserves the projection syntax, such as `[::2]`.
- `declared_type` and `declared_representation` describe the *write contract*.
  For projected places, this is the element contract, not the sequence contract.
- `lineage_sink` defaults to `owner` when omitted.

**PlaceSet helpers:**

- `uniform_type() -> object | None`
- `uniform_representation() -> object | None`
- `assert_non_empty()`
- `assert_uniform_contract()`

These helpers raise descriptive errors consumed by `can_setf()` and `setf()`.

### 2. `mlody/core/lineage.py`

**Purpose:** Centralize lineage event creation and append semantics.

**Public API:**

```python
def build_lineage_event(
    *,
    accessor: str,
    new_value: object,
    author: str | None,
    reason: str | None,
    timestamp: str | None,
    mode: AssignmentMode,
) -> object: ...

def append_lineage(value: object, event: object, *, mode: AssignmentMode) -> object: ...
```

**Design decisions:**

- The canonical event builder lives in `mlody/core/lineage.py`.
- The event format is a `Struct(kind="lineage_event", ...)` compatible with the
  existing `_lineage` list used on mlody values.
- The canonical accessor format is the string form of the normalized
  `PathExpression` fragment as applied to the place. This resolves the
  requirements open question about wildcard formatting: every stored accessor is
  the canonical serialized selector fragment after expansion or projection
  normalization.
- `append_lineage()` preserves existing `_lineage` contents and appends the new
  event at the end.

### 3. `mlody/core/setf_strategies.py`

**Purpose:** Provide the concrete v1 write strategies for in-memory mlody
values.

#### 3.1 `StructFieldSetter`

- Handles direct field writes on `Struct`-backed values.
- `preflight()` verifies the field exists and is writable.
- `commit(mode="copy")` rebuilds the owning struct with one substituted field.
- `commit(mode="inplace")` may still rebuild the owner and replace the reference
  in its parent chain; the public API must preserve mode distinction even if the
  underlying implementation is currently shared.

#### 3.2 `ListIndexSetter`

- Handles direct integer-index writes on Python list-backed values.
- Negative indices follow Python rules.
- Missing indices raise during preflight.

#### 3.3 `DictKeySetter`

- Handles direct key writes on Python dict-backed values.
- Keys must already exist; no implicit creation.

#### 3.4 `SequenceSliceSetter`

- Represents projected writes such as `[start:stop:step]`.
- Preflight resolves the concrete index set and verifies:
  - the owner is sequence-backed
  - every targeted element exists
  - the new value validates against the sequence element contract
- Commit writes the scalar or provided replacement value across the projection.
- Lineage is appended to the sequence owner with accessor `[start:stop:step]`.

#### 3.5 Expansion helpers

- `WildcardSegment` expands to a tuple of direct `Place` objects.
- `RecursiveDescentSegment` expands to a tuple of direct `Place` objects.
- These are resolved during place construction, not during commit.

**v1 segment policy:**

| Segment kind | v1 write behavior |
|--------------|-------------------|
| `FieldSegment` | Direct place |
| `IndexSegment` | Direct place |
| `KeySegment` | Direct place |
| `SliceSegment` | Projected place |
| `WildcardSegment` | Expands to many places |
| `RecursiveDescentSegment` | Expands to many places |
| `SqlSegment` | Out of scope; reserved for future backend |

### 4. `mlody/core/setf.py`

**Purpose:** Public orchestration entry point.

**Public API:**

```python
def resolve_places(root: object, selector: str | PathExpression) -> PlaceSet: ...
def can_setf(
    root: object,
    selector: str | PathExpression,
    new_value: object,
    *,
    mode: AssignmentMode = "inplace",
) -> None: ...
def setf(
    root: object,
    selector: str | PathExpression,
    new_value: object,
    *,
    mode: AssignmentMode = "inplace",
    author: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> object: ...
```

**Commit algorithm:**

1. Parse and normalize the selector.
2. Resolve a `PlaceSet`.
3. Assert the set is non-empty.
4. Assert uniform declared type across all places.
5. Assert uniform declared representation across all places.
6. Validate `new_value` once against the uniform contract.
7. Call `preflight()` on every place.
8. If all preflight checks succeed, call `commit()` on every place.
9. Append one lineage event per place to that place’s `lineage_sink`.
10. Return the updated root.

**Transaction rule:**

- If any step through (7) fails, no writes occur.
- If any commit step fails unexpectedly, the whole operation is an error. The
  initial implementation may rely on persistent rebuild semantics internally to
  make rollback trivial even in `inplace` mode.

### 5. Selector normalization and integration

**Purpose:** Reuse existing path semantics rather than fork them.

**Rules:**

- String selectors must parse through the existing traversal grammar.
- `PathExpression.__str__()` is the canonical serializer used for place
  accessors and lineage.
- Existing read-only traversal helpers in `mlody/resolver/label_value.py` and
  `mlody/core/virtual_value.py` remain authoritative for traversal semantics and
  should be factored where necessary into shared normalization helpers.

### 6. Type and representation validation

**v1 rule:** exact match only.

Two places are compatible for one bulk assignment only if:

1. their declared type objects compare equal, and
2. their declared representation objects compare equal.

The assigned value is then validated against that exact contract.

**Future hook:** compatibility is eventually both type-driven and
representation-driven, but that logic is deferred. The public API must not
assume exact-equality forever.

### 7. Virtual values and future backends

The initial implementation must define an extension seam for values whose
storage is not a simple in-memory `Struct`/list/dict:

- `resolve_places()` may delegate to backend-specific resolvers.
- `SetterStrategy` implementations may enforce copy-on-write or deny writes.
- `SqlSegment` remains reserved for a future selection backend and is not
  writable in this change.

This preserves the general `selector -> places -> preflight -> commit` model
without baking backend-specific behavior into `setf.py`.

---

## Data Architecture

### Place model

The write side introduces a new architectural entity not present on the read
side today: `Place`. `Place` is not persisted; it is the in-process carrier of:

- ownership
- current value
- write contract
- canonical accessor
- lineage sink
- strategy

### Lineage event shape

The event shape extends the current ad hoc lineage list with explicit update
metadata:

```python
Struct(
    kind="lineage_event",
    accessor="[::2]",
    new_value=42,
    author="mlody",
    timestamp="2026-04-20T12:00:00Z",
    reason="pin image sha",
    mode="inplace",
)
```

No old value is stored in the event.

---

## API Specifications

### `setf(...)`

**Request semantics:**

- `root`: root object or mlody value tree to update
- `selector`: string or `PathExpression`
- `new_value`: value assigned to every resolved place
- `mode`: `"inplace"` or `"copy"`
- `author`, `reason`, `timestamp`: lineage metadata

**Response semantics:**

- Returns the updated root object
- Raises:
  - parse error for invalid selectors
  - path error for missing paths
  - validation error for incompatible types / representations / values
  - write error for unsupported backends or denied writes

### `can_setf(...)`

Performs the same preflight as `setf(...)` but commits no writes and produces no
lineage.

### `resolve_places(...)`

Returns the `PlaceSet` for inspection, debugging, and future tooling.

---

## Implementation Plan

### Phase 1: Place model and public API

- Create `mlody/core/place.py`
- Create `mlody/core/setf.py`
- Add path normalization reuse

### Phase 2: In-memory strategies

- Implement direct setters for struct/list/dict
- Implement slice/projection semantics
- Implement wildcard / recursive-descent expansion

### Phase 3: Validation and lineage

- Add uniformity checks
- Add one-event-per-place lineage via `mlody/core/lineage.py`

### Phase 4: Hardening

- Add virtual-value extension seam
- Add error-shape cleanup
- Verify no regression in read-only traversal

### Deferred follow-up

- `.mlody` syntax lowering to `setf(...)`
- SQL-backed writable selection
- Compatibility conversion
- Backend permissions / copy-on-write policies

---

## Testing Strategy

### Unit tests

New test modules:

- `mlody/core/place_test.py`
- `mlody/core/setf_test.py`
- `mlody/core/lineage_test.py`
- `mlody/core/setf_strategies_test.py`

Minimum scenarios:

- parse selector string and resolve direct places
- field/index/key assignment succeeds
- slice assignment preserves aggregate accessor in lineage
- wildcard and recursive descent expand to multiple places
- heterogeneous type/representation sets fail in preflight
- failed preflight commits no writes
- `can_setf()` performs validation only
- `copy` mode returns rebuilt root
- `inplace` mode behaves identically at API level

### Integration tests

- Add resolver/workspace integration coverage where needed to confirm writable
  selection uses the same path semantics as readable selection.
- Add regression tests confirming read-only traversal is unchanged.

### Bazel targets

- Add `o_py_library` targets for new core modules.
- Add `o_py_test` targets for the new tests.
- Use Gazelle to update or create `BUILD.bazel` entries.

### Commands

- `bazel test //mlody/core:place_test`
- `bazel test //mlody/core:setf_test`
- `bazel test //mlody/core:lineage_test`
- `bazel test //mlody/core:setf_strategies_test`
- `bazel test //mlody/...`

---

## Deployment & Operations

No separate deployment is required. This is an in-process Python feature inside
the existing mlody runtime.

Operationally relevant behaviors:

- write failures must be explicit and deterministic
- preflight must avoid partial updates
- lineage generation must succeed for every committed place

---

## Non-Functional Requirements

- **Performance:** place resolution and preflight scale linearly with the number
  of selected places.
- **Reliability:** failed preflight leaves the root unchanged.
- **Maintainability:** selector parsing and normalization remain shared with the
  read path.
- **Compatibility:** existing read-only traversal APIs remain backward
  compatible.

---

## Risks & Mitigation

| Risk | Mitigation |
|------|------------|
| Mode semantics may diverge sharply for future backends | Preserve `inplace` vs `copy` as a first-class API choice now |
| Aggregate selectors may blur the meaning of “touched place” | Model projected places explicitly and attach lineage to the projection owner |
| Current value structures are conceptually immutable | Prefer persistent rebuild semantics internally, even when servicing `inplace` mode |
| Future writable backends may need authorization | Keep policy as a backend-specific strategy seam, not a special case in `setf.py` |

---

## Future Considerations

- Add `.mlody` assignment syntax that lowers directly to `setf(...)`.
- Add SQL as a selection backend once writable SQL semantics are specified.
- Relax exact-equality checks into compatibility-based coercion.
- Add backend policies for parquet and other external stores.
