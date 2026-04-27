"""Tests for the parquet-backed tabular source abstraction."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mlody.core.tabular.parquet_source import ParquetSource


def test_preview_returns_limited_rows_and_total_count(tmp_path: Path) -> None:
    parquet_path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"x": [1, 2, 3]}), parquet_path)

    source = ParquetSource(paths=(str(parquet_path),))
    preview = source.preview(2)

    assert preview.total_rows == 3
    assert preview.table.num_rows == 2
    assert preview.table.column_names == ["x"]


def test_count_unions_parquet_files_under_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "dataset"
    data_dir.mkdir()
    pq.write_table(pa.table({"x": [1, 2]}), data_dir / "part-000.parquet")
    pq.write_table(pa.table({"x": [3]}), data_dir / "part-001.parquet")

    source = ParquetSource(paths=(str(data_dir),))

    assert source.count() == 3


def test_schema_names_reads_first_available_file(tmp_path: Path) -> None:
    parquet_path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"x": [1], "y": [2]}), parquet_path)

    source = ParquetSource(paths=(str(parquet_path),))

    assert source.schema_names() == ("x", "y")


def test_schema_names_returns_empty_tuple_for_missing_sources() -> None:
    source = ParquetSource(paths=("missing/*.parquet",))

    assert source.schema_names() == ()
