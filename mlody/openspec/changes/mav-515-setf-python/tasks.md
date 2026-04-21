# Tasks: mav-515-setf-python

## 1. Foundation and module skeleton

- [x] 1.1 Create `mlody/core/place.py`, `mlody/core/setf.py`,
      `mlody/core/lineage.py`, and `mlody/core/setf_strategies.py` with module
      docstrings and typed public stubs
- [x] 1.2 Add or update `mlody/core/BUILD.bazel` using `o_py_library` /
      `o_py_test` targets for the new modules
- [x] 1.3 Run `bazel build //mlody/core:place //mlody/core:setf` (or the
      generated equivalent targets) to verify the skeleton builds

## 2. Place model and selector normalization

- [x] 2.1 Define `AssignmentMode`, `Place`, `PlaceSet`, and the
      `SetterStrategy` protocol in `mlody/core/place.py`
- [x] 2.2 Implement selector normalization so `resolve_places(...)` accepts
      either a string or `PathExpression`
- [x] 2.3 Reuse existing traversal grammar parsing and canonical
      `PathExpression.__str__()` serialization for place accessors
- [x] 2.4 Add unit tests for direct-place construction and canonical accessor
      formatting

## 3. In-memory setter strategies

- [x] 3.1 Implement `StructFieldSetter`
- [x] 3.2 Implement `ListIndexSetter`
- [x] 3.3 Implement `DictKeySetter`
- [x] 3.4 Implement `SequenceSliceSetter` for projected writes such as `[::2]`
- [x] 3.5 Implement wildcard expansion into multiple places
- [x] 3.6 Implement recursive-descent expansion into multiple places
- [x] 3.7 Add unit tests covering success and missing-path failures for all v1
      segment kinds

## 4. Validation and transaction-like preflight

- [x] 4.1 Implement `PlaceSet.assert_non_empty()`
- [x] 4.2 Implement exact-match uniformity checks for declared type and
      representation
- [x] 4.3 Implement `can_setf(...)` with full preflight and no commit
- [x] 4.4 Implement all-or-nothing commit orchestration in `setf(...)`
- [x] 4.5 Add tests proving heterogeneous target sets fail before any write
- [x] 4.6 Add tests proving failed preflight leaves the root unchanged

## 5. Lineage

- [x] 5.1 Implement `build_lineage_event(...)` in `mlody/core/lineage.py`
- [x] 5.2 Implement `append_lineage(...)` preserving existing `_lineage`
- [x] 5.3 Append one lineage event per touched place during successful `setf`
- [x] 5.4 Add tests for direct-place lineage
- [x] 5.5 Add tests for projected-place lineage preserving aggregate accessors
      such as `[::2]`

## 6. Integration and hardening

- [x] 6.1 Add integration points needed for virtual values or other non-trivial
      value shapes without changing read-only behavior
- [x] 6.2 Add regression tests confirming existing read traversal stays
      unchanged
- [x] 6.3 Run `bazel test //mlody/...`
- [ ] 6.4 Run `bazel build --config=lint //mlody/core/...` and fix all issues

Blocked: repo-wide lint analysis currently fails before target analysis in
`rules_nodejs` toolchain loading (`CcInfo` import issue in external
`nodejs/toolchain.bzl`).

## 7. Deferred follow-up change

- [x] 7.1 Specify `.mlody` surface syntax for assignment in a separate OpenSpec
      change after the Python engine stabilizes
- [x] 7.2 Specify SQL-backed writable selection in a separate OpenSpec change
      after `SqlSegment` write semantics are defined
- [x] 7.3 Specify compatibility conversion and backend permission policy in
      follow-up changes
