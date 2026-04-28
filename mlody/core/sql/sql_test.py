"""Tests for mlody.core.sql — all eight required scenarios (TEST-Q-001 through TEST-Q-008).

Fixture Parquet files are written to ``tmp_path`` (real filesystem) because
DuckDB uses C-level file access that pyfakefs cannot intercept.

Each test traces to a named scenario in the spec:
  spec.md / sql-query-interface/spec.md
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mlody.core.sql import MlodyQueryError, mlody_query
from mlody.core.sql.sql_query import _build_query, _normalize_paths, _read_columns


# ---------------------------------------------------------------------------
# Shared fixture: a small Parquet file with loss/epoch/label columns
# ---------------------------------------------------------------------------


@pytest.fixture()
def parquet_file(tmp_path: pytest.TempPathFactory) -> str:
    """Create a small Parquet fixture with 10 rows (loss, epoch, label).

    Returns the path as a string.
    """
    losses = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]
    epochs = list(range(1, 11))
    labels = ["a", "b", "a", "b", "a", "b", "a", "b", "a", "b"]

    table = pa.table(
        {
            "loss": pa.array(losses, type=pa.float64()),
            "epoch": pa.array(epochs, type=pa.int64()),
            "label": pa.array(labels, type=pa.string()),
        }
    )
    path = tmp_path / "train.parquet"
    pq.write_table(table, str(path))
    return str(path)


# ---------------------------------------------------------------------------
# Unit tests for _normalize_paths  (D-3)
# ---------------------------------------------------------------------------


def test_normalize_paths_single_str_returns_single_quoted_literal() -> None:
    """_normalize_paths wraps a str glob in single quotes."""
    result = _normalize_paths("data/*.parquet")
    assert result == "'data/*.parquet'"


def test_normalize_paths_single_path_returns_absolute_quoted(tmp_path: pytest.TempPathFactory) -> None:
    """_normalize_paths resolves a Path to absolute and single-quotes it."""
    from pathlib import Path

    p = Path(tmp_path) / "file.parquet"  # type: ignore[operator]
    result = _normalize_paths(p)
    assert result.startswith("'")
    assert result.endswith("'")
    # Absolute path
    assert str(p.resolve()) in result


def test_normalize_paths_list_returns_duckdb_list_literal() -> None:
    """_normalize_paths converts a list to a DuckDB ['a','b'] literal."""
    result = _normalize_paths(["a.parquet", "b.parquet"])
    assert result == "['a.parquet', 'b.parquet']"


# ---------------------------------------------------------------------------
# Unit tests for _read_columns  (D-4)
# ---------------------------------------------------------------------------


def test_read_columns_returns_schema_names(parquet_file: str) -> None:
    """_read_columns reads column names from the Parquet footer metadata."""
    cols = _read_columns(parquet_file)
    assert cols == ["loss", "epoch", "label"]


def test_read_columns_returns_empty_list_for_missing_file() -> None:
    """_read_columns returns [] (not an exception) when file does not exist."""
    cols = _read_columns("/nonexistent/path/file.parquet")
    assert cols == []


def test_read_columns_returns_empty_list_for_nonmatching_glob() -> None:
    """_read_columns returns [] when a glob matches no files."""
    cols = _read_columns("/definitely/not/there/*.parquet")
    assert cols == []


def test_read_columns_glob_reads_first_match(tmp_path: pytest.TempPathFactory) -> None:
    """_read_columns expands a glob and reads the first match."""
    table = pa.table({"x": pa.array([1, 2], type=pa.int32())})
    p = tmp_path / "data.parquet"  # type: ignore[operator]
    pq.write_table(table, str(p))
    # Glob that matches exactly one file
    cols = _read_columns(str(tmp_path) + "/*.parquet")
    assert cols == ["x"]


def test_read_columns_list_reads_first_element(parquet_file: str, tmp_path: pytest.TempPathFactory) -> None:
    """_read_columns reads schema from the first element of a list."""
    cols = _read_columns([parquet_file])
    assert cols == ["loss", "epoch", "label"]


# ---------------------------------------------------------------------------
# Unit tests for _build_query  (D-2, D-6)
# ---------------------------------------------------------------------------


def test_build_query_passes_through_select_statement() -> None:
    """SELECT queries are passed through unchanged (no injection)."""
    sql = "SELECT loss FROM read_parquet('x.parquet')"
    result = _build_query(sql, "'x.parquet'")
    assert result == sql


def test_build_query_passes_through_with_cte() -> None:
    """WITH (CTE) queries are passed through unchanged (D-6)."""
    sql = "WITH cte AS (SELECT 1 AS n) SELECT * FROM cte"
    result = _build_query(sql, "'x.parquet'")
    assert result == sql


def test_build_query_injects_select_for_where_clause() -> None:
    """A bare WHERE clause gets SELECT * FROM read_parquet(...) prepended."""
    result = _build_query("WHERE loss < 0.5", "'data.parquet'")
    assert result == "SELECT * FROM read_parquet('data.parquet') WHERE loss < 0.5"


def test_build_query_injects_select_for_sqlglot_parse_failure() -> None:
    """When sqlglot cannot parse the query, injection is applied (design.md R-1).

    A bare WHERE clause is a canonical example: sqlglot can't parse it as a
    standalone statement, so the query gets SELECT * prefixed.  DuckDB then
    validates the complete query, which may raise at execution time.
    """
    # 'SELEKT *' cannot be parsed by sqlglot; treat as a bare clause.
    result = _build_query("SELEKT *", "'x.parquet'")
    assert result == "SELECT * FROM read_parquet('x.parquet') SELEKT *"


# ---------------------------------------------------------------------------
# Integration tests — all eight required test scenarios (TEST-Q-001 through TEST-Q-008)
# ---------------------------------------------------------------------------


def test_q001_where_only_query_injects_select_star(parquet_file: str) -> None:
    """TEST-Q-001: WHERE-only query → SELECT * injected; all columns returned, rows filtered.

    Scenario: WHERE-only query gets SELECT * injected
    """
    result = mlody_query(paths=parquet_file, query="WHERE loss < 0.5")

    assert isinstance(result, pa.Table)
    # All three columns should be present (SELECT * injected)
    assert set(result.schema.names) == {"loss", "epoch", "label"}
    # loss < 0.5: values 0.4, 0.3, 0.2, 0.1, 0.05 → 5 rows
    losses = result.column("loss").to_pylist()
    assert all(v < 0.5 for v in losses)
    assert len(losses) == 5


def test_q002_explicit_select_returns_projected_columns(parquet_file: str) -> None:
    """TEST-Q-002: Explicit SELECT with column projection returns exactly two columns.

    Scenario: Explicit SELECT with column projection
    """
    query = f"SELECT loss, epoch FROM read_parquet('{parquet_file}')"
    result = mlody_query(paths=parquet_file, query=query)

    assert isinstance(result, pa.Table)
    assert result.schema.names == ["loss", "epoch"]
    assert result.num_rows == 10


def test_q003_full_select_with_where_filters_rows(parquet_file: str) -> None:
    """TEST-Q-003: Full SELECT * WHERE epoch > 5 returns only matching rows.

    Scenario: Full SELECT query returns pyarrow.Table
    """
    query = f"SELECT * FROM read_parquet('{parquet_file}') WHERE epoch > 5"
    result = mlody_query(paths=parquet_file, query=query)

    assert isinstance(result, pa.Table)
    epochs = result.column("epoch").to_pylist()
    assert all(e > 5 for e in epochs)
    assert len(epochs) == 5  # epochs 6, 7, 8, 9, 10


def test_q004_aggregation_group_by_returns_one_row_per_label(parquet_file: str) -> None:
    """TEST-Q-004: GROUP BY label → one row per distinct label with correct count.

    Scenario: Aggregation with explicit SELECT
    """
    query = (
        f"SELECT label, COUNT(*) AS n "
        f"FROM read_parquet('{parquet_file}') "
        f"GROUP BY label ORDER BY label"
    )
    result = mlody_query(paths=parquet_file, query=query)

    assert isinstance(result, pa.Table)
    assert result.num_rows == 2  # two distinct labels: 'a', 'b'
    labels = result.column("label").to_pylist()
    counts = result.column("n").to_pylist()
    assert set(zip(labels, counts)) == {("a", 5), ("b", 5)}


def test_q005_nonexistent_column_raises_mlody_query_error(parquet_file: str) -> None:
    """TEST-Q-005: Query referencing non-existent column → MlodyQueryError with all fields.

    Scenario: Invalid column name raises MlodyQueryError with all fields
    """
    query = f"SELECT nonexistent_col FROM read_parquet('{parquet_file}')"
    with pytest.raises(MlodyQueryError) as exc_info:
        mlody_query(paths=parquet_file, query=query)

    err = exc_info.value
    assert err.query == query
    assert "nonexistent_col" in err.expanded_query or err.expanded_query == query
    # columns must be populated from schema (not from DuckDB error message)
    assert "loss" in err.columns
    assert "epoch" in err.columns
    assert "label" in err.columns
    assert err.cause is not None


def test_q006_duckdb_syntax_error_raises_mlody_query_error(parquet_file: str) -> None:
    """TEST-Q-006: DuckDB-level syntax error → MlodyQueryError; raw exception does not propagate.

    Scenario: DuckDB syntax error raises MlodyQueryError
    Uses a query sqlglot accepts as a SELECT but DuckDB rejects at execution.
    """
    # This SELECT is syntactically valid enough for sqlglot to parse as Select,
    # but DuckDB will fail because 'INVALID_FUNC' does not exist.
    bad_query = f"SELECT INVALID_FUNC(loss, epoch) FROM read_parquet('{parquet_file}')"
    with pytest.raises(MlodyQueryError) as exc_info:
        mlody_query(paths=parquet_file, query=bad_query)

    err = exc_info.value
    assert isinstance(err, MlodyQueryError)
    assert err.query == bad_query
    assert err.cause is not None


def test_q007_multi_file_glob_returns_union_of_all_rows(tmp_path: pytest.TempPathFactory) -> None:
    """TEST-Q-007: Multi-file glob → result row count equals sum of both files.

    Scenario: Glob path queries all matching files
    """
    data_dir = tmp_path / "data"  # type: ignore[operator]
    data_dir.mkdir()

    table1 = pa.table({"val": pa.array([1, 2, 3], type=pa.int32())})
    table2 = pa.table({"val": pa.array([4, 5, 6, 7], type=pa.int32())})
    pq.write_table(table1, str(data_dir / "part_00.parquet"))
    pq.write_table(table2, str(data_dir / "part_01.parquet"))

    glob_path = str(data_dir / "*.parquet")
    result = mlody_query(paths=glob_path, query="WHERE val > 0")

    assert isinstance(result, pa.Table)
    # 3 rows from part_00 + 4 rows from part_01 = 7 rows total
    assert result.num_rows == 7


def test_q008_empty_result_returns_table_with_correct_schema(parquet_file: str) -> None:
    """TEST-Q-008: WHERE matching no rows → pa.Table with num_rows == 0; no exception.

    Scenario: Empty result set is not an error
    """
    query = f"SELECT * FROM read_parquet('{parquet_file}') WHERE loss < -9999"
    result = mlody_query(paths=parquet_file, query=query)

    assert isinstance(result, pa.Table)
    assert result.num_rows == 0
    # Schema must be correct even with zero rows
    assert set(result.schema.names) == {"loss", "epoch", "label"}


def test_mlody_query_supports_arrow_table_input_for_where_only_query() -> None:
    table = pa.table({"name": ["Alice", "Bob"], "age": [30, 40]})

    result = mlody_query(paths=table, query="WHERE age >= 40")

    assert isinstance(result, pa.Table)
    assert result.column_names == ["name", "age"]
    assert result.num_rows == 1
    assert result.column("name").to_pylist() == ["Bob"]


def test_mlody_query_supports_arrow_table_input_for_select_without_from() -> None:
    table = pa.table({"name": ["Alice", "Bob"], "age": [30, 40]})

    result = mlody_query(paths=table, query="SELECT name WHERE age >= 30")

    assert isinstance(result, pa.Table)
    assert result.column_names == ["name"]
    assert result.num_rows == 2


# ---------------------------------------------------------------------------
# MlodyQueryError __str__ and attribute tests  (tasks 2.1–2.3)
# ---------------------------------------------------------------------------


def test_mlody_query_error_str_includes_all_fields() -> None:
    """MlodyQueryError.__str__ includes query, expanded_query, and columns."""
    cause = ValueError("boom")
    err = MlodyQueryError(
        query="WHERE x = 1",
        expanded_query="SELECT * FROM read_parquet('f.parquet') WHERE x = 1",
        columns=["x", "y"],
        cause=cause,
    )
    text = str(err)
    assert "WHERE x = 1" in text
    assert "SELECT * FROM read_parquet" in text
    assert "x" in text
    assert "y" in text


def test_mlody_query_error_raise_from_sets_cause_chain() -> None:
    """raise MlodyQueryError(...) from cause sets __cause__ correctly."""
    cause = RuntimeError("original")
    try:
        raise MlodyQueryError(
            query="q",
            expanded_query="q",
            columns=[],
            cause=cause,
        ) from cause
    except MlodyQueryError as err:
        assert err.__cause__ is cause
        assert err.cause is cause
