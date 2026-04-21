## Why

Mlody can already parse rich traversal expressions and resolve them against
Starlark-backed values, virtual values, and some file-backed values. What it
cannot do is write back through those same selections. That gap blocks
configuration-oriented workflows where callers need to update one selected
setting or stamp the same value across a bulk selection, while preserving
inspectable provenance.

This change introduces a Python-first, Common Lisp `setf`-inspired assignment
engine for mlody. The engine treats selection as resolving one or more writable
places rather than only returning values. It then validates the entire
assignment up front and commits only when the whole operation is legal.

## What Changes

- Add a new Python assignment surface in `mlody/core/setf.py`:
  - `setf(root, selector, new_value, *, mode="inplace", author=None, reason=None, timestamp=None)`
  - `resolve_places(root, selector)`
  - `can_setf(root, selector, new_value)`
- Add a first-class writable-place model in `mlody/core/place.py`.
- Add a lineage helper in `mlody/core/lineage.py` so every successful write
  appends provenance to the touched value.
- Add in-memory setter strategies for:
  - Struct field assignment
  - list index assignment
  - dict key assignment
  - slice/projection assignment on sequences
  - wildcard / recursive-descent expansion into multiple places
- Reuse the existing traversal grammar and typed `PathSegment` hierarchy rather
  than inventing a write-only selector syntax.
- Keep SQL-backed selection and `.mlody` surface syntax out of scope for this
  change, but define extension seams so they can target the same engine later.

## Capabilities

### New Capabilities

- `generic-setf-engine`: Resolve writable places, validate transaction-like bulk
  updates, commit in either `inplace` or `copy` mode, and record lineage.

### Modified Capabilities

- `mlody value model`: Values with `_lineage` gain machine-generated update
  events produced by the new assignment engine.
- `traversal semantics`: Existing read traversal is unchanged, but the same path
  grammar now becomes the canonical selector surface for writable operations.

## Impact

- **New code:** `mlody/core/setf.py`, `mlody/core/place.py`,
  `mlody/core/lineage.py`, `mlody/core/setf_strategies.py` (or equivalent),
  plus tests.
- **Modified code:** likely `mlody/core/virtual_value.py`,
  `mlody/resolver/label_value.py`, and helper modules that currently own
  traversal or value-shape knowledge.
- **APIs:** new Python API only; no CLI or `.mlody` syntax changes in this
  change.
- **Compatibility:** existing read-only behavior must remain unchanged.
- **Future follow-ups:** `.mlody` syntax, SQL-backed selection, compatibility
  conversion, backend policy enforcement for writable external stores.
