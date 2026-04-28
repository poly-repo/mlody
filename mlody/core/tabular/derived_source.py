"""Typed derived tabular source implementation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mlody.core.optimiser import DerivedStep, QueryOptimiser, SequentialOptimiser
from mlody.core.sql.sql_query import mlody_query
from mlody.core.tabular.interfaces import PreviewResult, QueryInput
from mlody.core.tabular.location_specs import DerivedLocationSpec
from mlody.core.tabular.parquet_source import ParquetSource


class DerivedValueShapeError(ValueError):
    """Raised when a derived query produces a scalar (1×1) result."""

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


def validate_shape(table: pa.Table, *, sql_fragment: str = "") -> None:
    """Raise when a derived query result is scalar rather than tabular."""
    if table.num_rows == 1 and table.num_columns == 1:
        raise DerivedValueShapeError(
            sql_fragment=sql_fragment,
            num_rows=table.num_rows,
            num_columns=table.num_columns,
        )


@dataclass(frozen=True)
class DerivedSource:
    """A materializable derived source backed by a query over tabular input."""

    spec: DerivedLocationSpec
    optimiser: QueryOptimiser = field(default_factory=SequentialOptimiser)
    source_input: QueryInput | None = None

    def preview(self, limit: int) -> PreviewResult:
        """Return a parquet preview of the materialized derived output."""
        parquet_source = ParquetSource(paths=(str(self.materialize()),))
        preview = parquet_source.preview(limit)
        return PreviewResult(table=preview.table, total_rows=preview.total_rows)

    def count(self) -> int:
        """Return the total row count of the materialized derived output."""
        return ParquetSource(paths=(str(self.materialize()),)).count()

    def materialize(self) -> Path:
        """Materialize the derived dataset and return the cached parquet path."""
        output_path = self.spec.output_path
        if output_path.exists():
            return output_path

        step = DerivedStep(
            source_ref=self.spec.source_ref,
            sql_fragment=self.spec.query.sql,
            dialect=self.spec.query.dialect,
            output_path=str(output_path),
        )
        active_step = list(self.optimiser.optimise([step]))[0]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        query_source = self.source_input
        if query_source is None:
            if len(self.spec.source_paths) == 1:
                query_source = self.spec.source_paths[0]
            else:
                query_source = list(self.spec.source_paths)
        result = mlody_query(query_source, active_step.sql_fragment)
        validate_shape(result, sql_fragment=active_step.sql_fragment)

        tmp_path = Path(str(output_path) + ".tmp")
        try:
            pq.write_table(result, tmp_path)
            os.replace(tmp_path, output_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return output_path

    def query_input(self) -> QueryInput:
        """Return the concrete parquet output path as a queryable input."""
        return str(self.materialize())
