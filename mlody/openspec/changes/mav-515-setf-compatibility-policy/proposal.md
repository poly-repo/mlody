## Why

The current `setf` engine correctly insists on exact type and representation
matches, but that is intentionally conservative. Future writable backends also
need permission-aware policy decisions, especially for copy-on-write stores and
 external assets. Those concerns deserve their own change so the core engine can
stay strict and predictable in v1.

## What Changes

- Specify compatibility conversion hooks for type/representation-aware writes.
- Specify backend permission policy for writable selectors.
- Define how preflight reports conversion or permission failures.

## Impact

- Expands `can_setf(...)` and backend strategy contracts in a controlled,
  follow-up change.
- Keeps the v1 exact-match behavior unchanged until the policy work is ready.
