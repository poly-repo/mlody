## Why

The Python `setf` engine now provides the core place-resolution, validation,
bulk-update, and lineage semantics for writable path selection. Mlody still
lacks a source-language surface for invoking that engine inside `.mlody` files,
which blocks configuration-oriented workflows from expressing updates as first
class language operations.

## What Changes

- Add a `.mlody` assignment surface that lowers to the Python `setf` engine.
- Reuse the existing traversal grammar for selector strings instead of adding a
  second write-only selector language.
- Define author/reason/mode propagation from `.mlody` evaluation context into
  lineage events.

## Impact

- New follow-up spec for evaluator syntax and lowering rules.
- No changes to the Python engine contract; this change is a consumer of
  `mlody.core.setf`.
