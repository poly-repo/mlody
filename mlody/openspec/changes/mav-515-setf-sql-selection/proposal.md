## Why

The Python `setf` engine is selector-backend agnostic, but SQL-backed segments
are still read-only. Many future writable selections will be expressed through
SQL over typed datasets, not only parquet-backed values, so SQL write semantics
need their own change rather than being bolted onto the in-memory engine.

## What Changes

- Specify how `SqlSegment` selections resolve into writable places.
- Define bulk-assignment behavior for SQL result sets.
- Define how SQL-selected writes interact with backend copy-vs-inplace policy.

## Impact

- New selector backend contract for writable SQL places.
- No change to the transaction semantics already defined by
  `mav-515-setf-python`.
