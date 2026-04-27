"""Tests for mlody.core.derived — DerivedValueShapeError and materialise_derived.

Traces to openspec/changes/value-source-query/specs/source-query-materialisation/spec.md
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mlody.core.derived import DerivedValueShapeError, materialise_derived
from mlody.core.optimiser import DerivedStep
from mlody.core.tabular.interfaces import QuerySpec
from mlody.core.tabular.location_specs import DerivedLocationSpec


def _make_spec(
    source_ref: str,
    source_paths: tuple[str, ...],
    sql_fragment: str,
    output_path: str,
    dialect: str = "duckdb",
) -> DerivedLocationSpec:
    """Create a typed derived spec for materialization tests."""
    return DerivedLocationSpec(
        source_ref=source_ref,
        source_paths=source_paths,
        query=QuerySpec(sql=sql_fragment, dialect=dialect),
        output_path=Path(output_path),
    )


# ---------------------------------------------------------------------------
# Tests for validate_shape (Task 4.4)
# ---------------------------------------------------------------------------


class TestValidateShape:
    """Requirement: Shape validation rejects scalar results."""

    def test_zero_row_table_is_accepted(self, tmp_path: Path) -> None:
        # Scenario: zero-row result is accepted
        from mlody.core.derived import validate_shape

        schema = pa.schema([pa.field("x", pa.int64())])
        table = pa.table({"x": pa.array([], type=pa.int64())})
        # No exception should be raised
        validate_shape(table)

    def test_one_by_one_scalar_raises_shape_error(self) -> None:
        # Scenario: 1x1 scalar result raises DerivedValueShapeError
        from mlody.core.derived import validate_shape

        table = pa.table({"count": [42]})
        assert table.num_rows == 1 and table.num_columns == 1
        with pytest.raises(DerivedValueShapeError) as exc_info:
            validate_shape(table)
        err = exc_info.value
        assert err.num_rows == 1
        assert err.num_columns == 1

    def test_shape_error_carries_sql_fragment(self) -> None:
        # DerivedValueShapeError carries the sql_fragment passed at raise time
        from mlody.core.derived import validate_shape

        table = pa.table({"count": [5]})
        with pytest.raises(DerivedValueShapeError) as exc_info:
            validate_shape(table, sql_fragment="SELECT COUNT(*)")
        assert exc_info.value.sql_fragment == "SELECT COUNT(*)"

    def test_one_row_two_columns_is_accepted(self) -> None:
        # Scenario: multi-column 1-row result is accepted
        from mlody.core.derived import validate_shape

        table = pa.table({"a": [1], "b": [2]})
        assert table.num_rows == 1 and table.num_columns == 2
        validate_shape(table)  # should not raise

    def test_multi_row_table_is_accepted(self) -> None:
        # Multi-row result is always accepted
        from mlody.core.derived import validate_shape

        table = pa.table({"x": [1, 2, 3], "y": [4, 5, 6]})
        validate_shape(table)  # should not raise


# ---------------------------------------------------------------------------
# Tests for materialise_derived (Task 4.5)
# ---------------------------------------------------------------------------


class TestMaterialiseDerived:
    """Requirement: Materialise derived location on cache miss."""

    def test_cache_miss_triggers_query_and_write(self, tmp_path: Path) -> None:
        # Scenario: cache miss triggers query execution and write
        output_path = str(tmp_path / "output.parquet")
        source_paths = (str(tmp_path / "source.parquet"),)
        spec = _make_spec(
            source_ref=":data",
            source_paths=source_paths,
            sql_fragment="WHERE x > 0",
            output_path=output_path,
        )
        result_table = pa.table({"x": [1, 2, 3]})

        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            return_value=result_table,
        ) as mock_query:
            returned = materialise_derived(spec)

        mock_query.assert_called_once()
        assert returned == output_path
        assert Path(output_path).exists()
        loaded = pq.read_table(output_path)
        assert loaded.num_rows == 3

    def test_cache_hit_skips_query(self, tmp_path: Path) -> None:
        # Scenario: cache hit skips re-execution
        output_path = str(tmp_path / "cached.parquet")
        # Pre-create the output file (simulating a cache hit)
        existing_table = pa.table({"x": [99]})
        pq.write_table(existing_table, output_path)

        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="WHERE x > 0",
            output_path=output_path,
        )

        with patch("mlody.core.tabular.derived_source.mlody_query") as mock_query:
            returned = materialise_derived(spec)

        mock_query.assert_not_called()
        assert returned == output_path

    def test_parent_dir_created_if_absent(self, tmp_path: Path) -> None:
        # Scenario: parent cache directory is created if absent
        nested_dir = tmp_path / "a" / "b" / "c"
        output_path = str(nested_dir / "output.parquet")
        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="WHERE x > 0",
            output_path=output_path,
        )
        # Use a 2-column table to avoid the 1×1 scalar rejection.
        result_table = pa.table({"x": [1], "y": [2]})

        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            return_value=result_table,
        ):
            materialise_derived(spec)

        assert nested_dir.exists()
        assert Path(output_path).exists()

    def test_write_failure_leaves_no_partial_file(self, tmp_path: Path) -> None:
        # Scenario: write failure leaves no partial file
        output_path = str(tmp_path / "output.parquet")
        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="WHERE x > 0",
            output_path=output_path,
        )
        result_table = pa.table({"x": [1, 2]})

        # Simulate write failure by patching pq.write_table
        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            return_value=result_table,
        ), patch(
            "mlody.core.tabular.derived_source.pq.write_table",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError, match="disk full"):
                materialise_derived(spec)

        # Neither the output nor the .tmp file should remain
        assert not Path(output_path).exists()
        assert not Path(output_path + ".tmp").exists()

    def test_mlody_query_error_propagates(self, tmp_path: Path) -> None:
        # Scenario: invalid SQL propagates MlodyQueryError
        from mlody.core.sql.sql_query import MlodyQueryError

        output_path = str(tmp_path / "output.parquet")
        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="INVALID SQL !!!",
            output_path=output_path,
        )
        error = MlodyQueryError(
            query="INVALID SQL !!!",
            expanded_query="INVALID SQL !!!",
            columns=[],
            cause=Exception("syntax error"),
        )

        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            side_effect=error,
        ):
            with pytest.raises(MlodyQueryError):
                materialise_derived(spec)

    def test_custom_optimiser_is_invoked(self, tmp_path: Path) -> None:
        # Scenario: custom optimiser is invoked before query execution
        output_path = str(tmp_path / "output.parquet")
        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="WHERE x > 0",
            output_path=output_path,
        )
        # Use a 2-column table to avoid the 1×1 scalar rejection.
        result_table = pa.table({"x": [1], "y": [2]})

        class UpperCaseOptimiser:
            """Transforms sql_fragment to uppercase to prove it is called."""

            def optimise(
                self, steps: list[DerivedStep]
            ) -> list[DerivedStep]:
                return [
                    DerivedStep(
                        source_ref=s.source_ref,
                        sql_fragment=s.sql_fragment.upper(),
                        dialect=s.dialect,
                        output_path=s.output_path,
                    )
                    for s in steps
                ]

        captured_query: list[str] = []

        def capturing_query(paths: object, query: str) -> pa.Table:
            captured_query.append(query)
            return result_table

        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            side_effect=capturing_query,
        ):
            materialise_derived(
                spec,
                optimiser=UpperCaseOptimiser(),
            )

        assert len(captured_query) == 1
        assert captured_query[0] == "WHERE X > 0"

    def test_shape_error_raised_on_1x1_result(self, tmp_path: Path) -> None:
        # Scenario: 1x1 scalar result raises DerivedValueShapeError during materialisation
        output_path = str(tmp_path / "output.parquet")
        spec = _make_spec(
            source_ref=":data",
            source_paths=(str(tmp_path / "source.parquet"),),
            sql_fragment="SELECT COUNT(*)",
            output_path=output_path,
        )
        scalar_table = pa.table({"count": [42]})

        with patch(
            "mlody.core.tabular.derived_source.mlody_query",
            return_value=scalar_table,
        ):
            with pytest.raises(DerivedValueShapeError) as exc_info:
                materialise_derived(spec)

        err = exc_info.value
        assert err.num_rows == 1
        assert err.num_columns == 1
        assert "SELECT COUNT(*)" in err.sql_fragment
