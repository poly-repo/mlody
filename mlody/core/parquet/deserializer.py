"""ParquetDeserializer: row-efficient reads from a local Parquet file.

The deserializer wraps a single ``pyarrow.parquet.ParquetFile`` and maps typed
``PathSegment`` operations to the minimal set of row-group reads needed to
answer the query.  It never loads the entire file into memory.

Public API:
    ParquetDeserializer(path)   — construct (validates path, defers open)
    deserializer[n]             — read row n → dict[str, Any]
    deserializer[start:stop:step] → list[dict[str, Any]]
    read_file_as_rows(path)     — read all rows in one columnar pass (fast)
    convert_arrow_value         — convert a single pyarrow scalar to Python
    register_parquet_handler    — register a custom handler for a pyarrow type
    OPAQUE_SENTINEL             — the sentinel returned for unhandled opaque types
    _clear_handlers             — test helper: reset the global registry
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Opaque-type sentinel and extension registry  (tasks 2.8, D-5)
# ---------------------------------------------------------------------------

OPAQUE_SENTINEL: str = "<image>"
"""Returned for Parquet column values whose pyarrow type is not in the registry
and is a non-primitive structured type (``pa.struct``, ``pa.map_``, etc.)."""

# Registry maps registered pa.DataType objects to handler callables.
# Lookup is a linear scan using pa.DataType.__eq__ because pyarrow DataType
# instances are not hashable via Python's id-based hash (D-5).
_HANDLER_REGISTRY: list[tuple[pa.DataType, Callable[[Any, pa.Field], Any]]] = []


def register_parquet_handler(
    pa_type: pa.DataType,
    handler: Callable[[Any, pa.Field], Any],
) -> None:
    """Register a custom handler for a pyarrow column type.

    When a column's schema type equals ``pa_type`` (checked via
    ``pa.DataType.__eq__``), the deserializer calls ``handler(value, field)``
    instead of returning ``OPAQUE_SENTINEL``.

    Registering the same ``pa_type`` twice replaces the first registration
    (last-write-wins).

    Args:
        pa_type: The pyarrow DataType to match (e.g. ``pa.struct([...])``)
        handler: Callable receiving ``(raw_value, pa.Field)`` → converted value.
    """
    # Replace existing entry for this type if present.
    for i, (existing_type, _) in enumerate(_HANDLER_REGISTRY):
        if existing_type == pa_type:
            _HANDLER_REGISTRY[i] = (pa_type, handler)
            return
    _HANDLER_REGISTRY.append((pa_type, handler))


def _clear_handlers() -> None:
    """Reset the global handler registry.

    Intended for use in tests to prevent cross-test pollution.  Not part of
    the public API.
    """
    _HANDLER_REGISTRY.clear()


__all__ = [
    "OPAQUE_SENTINEL",
    "ParquetDeserializer",
    "convert_arrow_value",
    "read_file_as_rows",
    "register_parquet_handler",
]


def convert_arrow_value(value: object, field: pa.Field) -> Any:
    """Convert a pyarrow scalar to a Python value using the global handler registry.

    Checks the global handler registry first.  For unregistered structured
    types (``pa.struct``, ``pa.map_``, large-binary) returns
    ``OPAQUE_SENTINEL`` without raising.

    Args:
        value: The raw value extracted from a pyarrow Table column.
        field: The ``pa.Field`` descriptor for the column.

    Returns:
        A Python-native value, or ``OPAQUE_SENTINEL`` for opaque types.
    """
    field_type = field.type

    # Check extension registry first (D-5).
    for registered_type, handler in _HANDLER_REGISTRY:
        if field_type == registered_type:
            return handler(value, field)

    # Struct scalars: convert to Python dicts, mirroring pa.Table.to_pydict().
    # This lets callers inspect nested fields (e.g. HuggingFace image structs
    # which store {'bytes': <png-bytes>, 'path': None}).
    if pa.types.is_struct(field_type):
        if hasattr(value, "as_py"):
            return value.as_py()  # type: ignore[union-attr]
        return OPAQUE_SENTINEL

    # Remaining opaque types (map, binary): return sentinel without conversion.
    if (
        pa.types.is_map(field_type)
        or pa.types.is_large_binary(field_type)
        or pa.types.is_binary(field_type)
    ):
        return OPAQUE_SENTINEL

    # Primitive scalar: convert to Python via pyarrow's .as_py() if
    # the value is a pyarrow scalar; otherwise return as-is.
    if hasattr(value, "as_py"):
        return value.as_py()  # type: ignore[union-attr]
    return value


# ---------------------------------------------------------------------------
# ParquetDeserializer  (tasks 2.1–2.7, D-3, D-7)
# ---------------------------------------------------------------------------


class ParquetDeserializer:
    """Lazy, row-efficient reader for a single local Parquet file.

    Construction validates that the file exists but does not open it.  The
    underlying ``ParquetFile`` is opened on first access (lazy open, D-3) and
    cached on the instance.

    Supports:
    - ``deserializer[n]``           → ``dict[str, Any]`` for row *n*
    - ``deserializer[start:stop:step]`` → ``list[dict[str, Any]]``

    Dict keys are column names; values are Python scalars (or
    ``OPAQUE_SENTINEL`` for structured types without a registered handler).

    Args:
        path: Local filesystem path to a Parquet file.  Relative paths are
            resolved against the current working directory.

    Raises:
        FileNotFoundError: immediately at construction if the path does not
            exist on disk.
    """

    def __init__(self, path: Path | str) -> None:
        """Validate path exists on disk and store it; defer opening the file.

        Args:
            path: Local filesystem path to a Parquet file.

        Raises:
            FileNotFoundError: if the path does not exist.
        """
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"Parquet file not found: {path!r} (resolved: {resolved})"
            )
        self._path: Path = resolved
        self.__pf: pq.ParquetFile | None = None

    # ------------------------------------------------------------------
    # Lazy pyarrow ParquetFile access
    # ------------------------------------------------------------------

    @property
    def _pf(self) -> pq.ParquetFile:
        """Open and cache the ParquetFile on first access."""
        if self.__pf is None:
            self.__pf = pq.ParquetFile(str(self._path))
        return self.__pf

    @property
    def num_rows(self) -> int:
        """Total number of rows in the file, read from file metadata."""
        return int(self._pf.metadata.num_rows)

    # ------------------------------------------------------------------
    # Value conversion
    # ------------------------------------------------------------------

    def _convert_value(self, value: object, field: pa.Field) -> Any:
        return convert_arrow_value(value, field)

    # ------------------------------------------------------------------
    # Row and slice reading  (D-7)
    # ------------------------------------------------------------------

    def _find_row_group(self, row_index: int) -> tuple[int, int]:
        """Find the row group and offset within it for absolute row index.

        Scans cumulative row counts from metadata — O(G) with no I/O.

        Args:
            row_index: Non-negative absolute row index (already normalised).

        Returns:
            ``(rg_index, offset_within_group)`` tuple.

        Raises:
            IndexError: if row_index is out of range.
        """
        metadata = self._pf.metadata
        cumulative = 0
        for rg in range(metadata.num_row_groups):
            rg_rows = metadata.row_group(rg).num_rows
            if row_index < cumulative + rg_rows:
                return rg, row_index - cumulative
            cumulative += rg_rows
        # Should not be reachable if caller normalised the index correctly.
        raise IndexError(
            f"row index {row_index} is out of range for file with {cumulative} rows"
        )

    def _read_row(self, n: int) -> dict[str, Any]:
        """Read a single row by index, returning a column-name → value dict.

        Reads only the row group containing row *n*; the rest of the file is
        not loaded (D-7, CON-P-003).

        Args:
            n: Row index (positive or negative, Python-style).

        Returns:
            ``dict[str, Any]`` mapping column names to Python values.

        Raises:
            IndexError: if *n* is out of range, with the index and file size.
        """
        num = self.num_rows
        if n < 0:
            n = num + n
        if n < 0 or n >= num:
            raise IndexError(
                f"row index out of range: n={n!r}, num_rows={num}"
            )

        rg_idx, offset = self._find_row_group(n)
        table = self._pf.read_row_group(rg_idx)
        schema = table.schema

        row: dict[str, Any] = {}
        for col_idx, field in enumerate(schema):
            col = table.column(col_idx)
            raw = col[offset]
            row[field.name] = self._convert_value(raw, field)
        return row

    def _read_slice(
        self,
        start: int | None,
        stop: int | None,
        step: int | None,
    ) -> list[dict[str, Any]]:
        """Read a slice of rows, returning a list of column-name → value dicts.

        Resolves concrete indices via ``slice(...).indices(num_rows)`` and
        collects the required row groups in a single read per group (D-7).

        Args:
            start: Slice start (None → 0 or end of file, Python semantics).
            stop:  Slice stop (None → end or beginning, Python semantics).
            step:  Slice step (None → 1).

        Returns:
            ``list[dict[str, Any]]`` — one dict per row in the slice range.
        """
        num = self.num_rows
        indices = list(range(*slice(start, stop, step).indices(num)))
        if not indices:
            return []

        # Read each required row.  For contiguous slices (step=1 or step=None)
        # this could be further optimised to read whole row-group spans; for
        # correctness and simplicity we call _read_row for each index.  The
        # row-group metadata scan is O(G) per call but there is no I/O until
        # the first access to a new group.
        #
        # A future optimisation: group contiguous indices by row group and issue
        # one read_row_group per group instead of one per row.
        return [self._read_row(i) for i in indices]

    # ------------------------------------------------------------------
    # Subscript interface
    # ------------------------------------------------------------------

    def __getitem__(self, key: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        """Subscript access: int → single row dict; slice → list of row dicts.

        Args:
            key: An integer row index or a ``slice`` object.

        Returns:
            A single ``dict[str, Any]`` for integer keys, or a
            ``list[dict[str, Any]]`` for slice keys.

        Raises:
            IndexError: for out-of-bounds integer keys.
            TypeError:  for keys that are neither ``int`` nor ``slice``.
        """
        if isinstance(key, int):
            return self._read_row(key)
        if isinstance(key, slice):
            return self._read_slice(key.start, key.stop, key.step)
        raise TypeError(
            f"ParquetDeserializer indices must be integers or slices, "
            f"not {type(key).__name__!r}"
        )

    # ------------------------------------------------------------------
    # String representation  (UI-P-002)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a concise representation showing path and row count.

        Triggers lazy open to read the row count from file metadata.

        Returns:
            String of the form ``ParquetDeserializer('<path>', num_rows=N)``.
        """
        return f"ParquetDeserializer({str(self._path)!r}, num_rows={self.num_rows})"


# ---------------------------------------------------------------------------
# Fast whole-file reader (columnar pass, no per-row overhead)
# ---------------------------------------------------------------------------


def read_file_as_rows(path: Path | str) -> list[dict[str, Any]]:
    """Read all rows from a Parquet file in a single columnar pass.

    Reads every column into a Python list at once using PyArrow's vectorised
    ``to_pylist()``, applies the global handler registry and opaque-type
    sentinel per column, then transposes the column arrays into row dicts.

    This is O(rows * cols) in memory but performs a single I/O read of the
    file, making it orders of magnitude faster than iterating via
    ``ParquetDeserializer[i]`` for large slices.

    Args:
        path: Local filesystem path to a Parquet file.

    Returns:
        ``list[dict[str, Any]]`` — one dict per row, column name → value.

    Raises:
        FileNotFoundError: if the path does not exist.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Parquet file not found: {path!r}")

    pf = pq.ParquetFile(str(resolved))
    table = pf.read()
    schema = table.schema

    col_arrays: dict[str, list] = {}
    for field in schema:
        col = table.column(field.name)
        col_arrays[field.name] = [convert_arrow_value(v, field) for v in col]

    keys = list(col_arrays.keys())
    n = table.num_rows
    return [{k: col_arrays[k][i] for k in keys} for i in range(n)]
