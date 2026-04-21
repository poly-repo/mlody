# SPEC: Compatibility Conversion and Backend Permission Policy

**Status:** Draft  
**Depends on:** `mav-515-setf-python`

## Summary

This change introduces two opt-in extensions to the v1 `setf` engine:

- compatibility conversion when an assigned value is not an exact type or
  representation match but can be safely adapted
- backend permission policy for writable targets whose commit mode has external
  side effects

## Goals

- Keep conversion and permission logic outside the core place model.
- Preserve preflight-first, commit-second transaction semantics.
- Let backends distinguish between `inplace`, `copy`, and denied writes.

## Non-Goals

- Weakening v1 exact-match defaults globally.
- Defining SQL writable selection semantics; that belongs to the SQL follow-up.

## Proposed Model

### Compatibility hook

- Each writable backend may expose a compatibility adapter during preflight.
- If exact match fails, the adapter may either:
  - produce a converted value, or
  - reject the write with a structured compatibility error.

### Permission hook

- Each writable backend may expose a policy decision for the requested mode:
  - `allow_inplace`
  - `allow_copy_only`
  - `deny`

## Open Questions

- Where should converted values be recorded in lineage events?
- Should permission denials surface as `PermissionError` or a mlody-specific
  error type?
- Does compatibility checking happen once per uniform contract or once per
  concrete place?

