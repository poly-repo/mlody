"""Tests for the derived tabular source abstraction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mlody.core.tabular.derived_source import DerivedSource, DerivedValueShapeError
from mlody.core.tabular.interfaces import QuerySpec
from mlody.core.tabular.location_specs import DerivedLocationSpec


def _spec(tmp_path: Path, *, sql: str = "WHERE x > 0") -> DerivedLocationSpec:
    return DerivedLocationSpec(
        source_ref=":upstream",
        source_paths=(str(tmp_path / "source.parquet"),),
        query=QuerySpec(sql=sql, dialect="duckdb"),
        output_path=tmp_path / "derived.parquet",
    )


def test_materialize_cache_miss_executes_query_and_writes_output(tmp_path: Path) -> None:
    source = DerivedSource(spec=_spec(tmp_path))
    result_table = pa.table({"x": [1, 2, 3], "y": [4, 5, 6]})

    with patch(
        "mlody.core.tabular.derived_source.mlody_query",
        return_value=result_table,
    ) as mock_query:
        output_path = source.materialize()

    mock_query.assert_called_once_with(str(tmp_path / "source.parquet"), "WHERE x > 0")
    assert output_path == tmp_path / "derived.parquet"
    assert output_path.exists()
    assert pq.read_table(output_path).num_rows == 3


def test_materialize_cache_hit_skips_query_execution(tmp_path: Path) -> None:
    output_path = tmp_path / "derived.parquet"
    pq.write_table(pa.table({"x": [99], "y": [100]}), output_path)
    source = DerivedSource(spec=_spec(tmp_path))

    with patch("mlody.core.tabular.derived_source.mlody_query") as mock_query:
        returned = source.materialize()

    mock_query.assert_not_called()
    assert returned == output_path


def test_materialize_rejects_scalar_results(tmp_path: Path) -> None:
    source = DerivedSource(spec=_spec(tmp_path, sql="SELECT COUNT(*)"))
    scalar_result = pa.table({"count": [42]})

    with patch(
        "mlody.core.tabular.derived_source.mlody_query",
        return_value=scalar_result,
    ):
        with pytest.raises(DerivedValueShapeError):
            source.materialize()


def test_preview_uses_materialized_output_for_preview_and_count(tmp_path: Path) -> None:
    output_path = tmp_path / "derived.parquet"
    pq.write_table(pa.table({"x": [1, 2, 3]}), output_path)
    source = DerivedSource(spec=_spec(tmp_path))

    preview = source.preview(2)

    assert preview.total_rows == 3
    assert preview.table.num_rows == 2


def test_materialize_accepts_arrow_table_source_input(tmp_path: Path) -> None:
    source = DerivedSource(
        spec=_spec(tmp_path, sql="WHERE age >= 40"),
        source_input=pa.table({"name": ["Alice", "Bob"], "age": [30, 40]}),
    )

    output_path = source.materialize()

    assert output_path.exists()
    assert pq.read_table(output_path).column("name").to_pylist() == ["Bob"]
