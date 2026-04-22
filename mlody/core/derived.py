"""Materialisation of derived values — cache-hit/miss logic, shape validation.

Public surface:
- ``DerivedValueShapeError`` — raised when a query result is a scalar (1×1)
- ``validate_shape(table, *, sql_fragment="")`` — check result table shape
- ``materialise_derived(location, source_paths, *, optimiser=...)`` — execute
  and cache a derived value, returning the output path
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from mlody.core.optimiser import DerivedStep, QueryOptimiser, SequentialOptimiser
from mlody.core.sql.sql_query import MlodyQueryError, mlody_query

if TYPE_CHECKING:
    pass

_DEFAULT_OPTIMISER = SequentialOptimiser()


# ---------------------------------------------------------------------------
# DerivedValueShapeError
# ---------------------------------------------------------------------------


class DerivedValueShapeError(ValueError):
    """Raised when a derived-value query returns a 1×1 scalar table.

    A 1×1 result (e.g. ``SELECT COUNT(*)``) has no useful row-dataset
    semantics and is rejected to prevent silently producing a degenerate
    Parquet file.  Zero-row results are valid and accepted.

    Attributes:
        sql_fragment: The SQL text that produced the rejected result.
        num_rows: Number of rows in the rejected table.
        num_columns: Number of columns in the rejected table.
    """

    def __init__(
        self,
        *,
        sql_fragment: str,
        num_rows: int,
        num_columns: int,
    ) -> None:
        super().__init__(
            f"Derived query produced a scalar (1×1) result which cannot be "
            f"stored as a row dataset — use a query that returns multiple "
            f"columns or filters rows.  sql_fragment={sql_fragment!r}, "
            f"num_rows={num_rows}, num_columns={num_columns}"
        )
        self.sql_fragment = sql_fragment
        self.num_rows = num_rows
        self.num_columns = num_columns


# ---------------------------------------------------------------------------
# validate_shape
# ---------------------------------------------------------------------------


def validate_shape(table: pa.Table, *, sql_fragment: str = "") -> None:
    """Raise ``DerivedValueShapeError`` if ``table`` is a scalar (1×1) result.

    A 1×1 table (exactly 1 row and 1 column) is treated as a scalar and
    rejected.  Zero-row tables are valid.  All other shapes are valid.

    Args:
        table: The query result table to inspect.
        sql_fragment: The SQL text used to produce the result.  Carried on
            the raised exception for diagnostics.

    Raises:
        DerivedValueShapeError: When ``table.num_rows == 1 and
            table.num_columns == 1``.
    """
    if table.num_rows == 1 and table.num_columns == 1:
        raise DerivedValueShapeError(
            sql_fragment=sql_fragment,
            num_rows=table.num_rows,
            num_columns=table.num_columns,
        )


# ---------------------------------------------------------------------------
# materialise_derived
# ---------------------------------------------------------------------------


def materialise_derived(
    location: object,
    source_paths: str | Path | list[str | Path],
    *,
    optimiser: QueryOptimiser = _DEFAULT_OPTIMISER,
) -> str:
    """Materialise a derived value, returning its cached Parquet path.

    On cache hit (``output_path`` file already exists) returns immediately.
    On cache miss: runs the SQL fragment via ``mlody_query``, validates the
    result shape, and writes the result atomically to ``output_path``
    (writing to a ``.tmp`` sibling first, then renaming).

    The ``optimiser`` is always called before execution; the default
    ``SequentialOptimiser`` returns the step unchanged.

    Args:
        location: A derived location struct (or any object with an
            ``attributes`` dict containing ``source_ref``, ``sql_fragment``,
            ``dialect``, and ``output_path``).
        source_paths: Resolved source Parquet path(s).  Passed directly to
            ``mlody_query``.
        optimiser: A ``QueryOptimiser`` implementation.  Defaults to
            ``SequentialOptimiser()`` (no-op).

    Returns:
        The ``output_path`` string from the location.

    Raises:
        DerivedValueShapeError: When the query produces a scalar (1×1) result.
        MlodyQueryError: When ``mlody_query`` raises (invalid SQL, missing
            source, etc.).  Not wrapped.
    """
    attrs: dict[str, str] = location.attributes  # type: ignore[union-attr]
    sql_fragment = attrs["sql_fragment"]
    dialect = attrs["dialect"]
    output_path = attrs["output_path"]
    source_ref = attrs["source_ref"]

    # Cache hit: file already exists; return immediately without re-executing.
    if Path(output_path).exists():
        return output_path

    # Build the DerivedStep and pass it through the optimiser.
    step = DerivedStep(
        source_ref=source_ref,
        sql_fragment=sql_fragment,
        dialect=dialect,
        output_path=output_path,
    )
    steps = optimiser.optimise([step])
    # materialise_derived handles exactly one step (one derived location).
    active_step = list(steps)[0]

    # Ensure the parent directory exists.
    parent = Path(output_path).parent
    parent.mkdir(parents=True, exist_ok=True)

    # Execute the query.  MlodyQueryError propagates unchanged.
    result: pa.Table = mlody_query(source_paths, active_step.sql_fragment)

    # Validate shape: reject 1×1 scalar results.
    validate_shape(result, sql_fragment=active_step.sql_fragment)

    # Atomic write: write to .tmp then rename.
    tmp_path = output_path + ".tmp"
    try:
        pq.write_table(result, tmp_path)
        os.replace(tmp_path, output_path)
    except Exception:
        # Clean up partial write so no stale .tmp file remains.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return output_path
