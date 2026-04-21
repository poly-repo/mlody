# SPEC: SQL-Backed Writable Selection

**Status:** Draft  
**Depends on:** `mav-515-setf-python`

## Summary

This change extends place resolution so SQL-backed selectors can participate in
`setf` operations. SQL remains just another selector backend:

`selector -> concrete places -> validate all -> commit all`

The SQL layer is responsible for translating query results into concrete places
with stable write handles.

## Goals

- Treat SQL result sets as bulk assignment targets.
- Preserve all-or-nothing preflight semantics across selected rows/cells.
- Keep SQL writable selection independent of any single storage backend.

## Non-Goals

- Direct file-permission policy.
- Automatic type conversion.
- `.mlody` source syntax beyond what the syntax follow-up defines.

## Design Constraints

- SQL selection must produce deterministic, concrete places before commit.
- Every selected place in a bulk write must still share an exact v1
  type/representation contract.
- Backends may reject writes during preflight if they cannot provide stable
  write handles.

## Open Questions

- What is the canonical write handle for SQL-selected regions?
- How are overlapping SQL selections normalized before commit?
- Should SQL-backed writes require an explicit opt-in on each backend?

