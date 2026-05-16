"""Tests for ParquetDeserializer — mlody-parquet-traversal spec.

Covers:
- TEST-P-001: row index and slice access
- TEST-P-002: field access on dict / list-of-dicts
- TEST-P-003: chained traversal
- TEST-P-004: opaque-type sentinel
- TEST-P-005: extension registry
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mlody.core.parquet.deserializer import (
    OPAQUE_SENTINEL,
    ParquetDeserializer,
    _clear_handlers,
    convert_arrow_value,
    register_parquet_handler,
)
from mlody.core.parquet import (
    OPAQUE_SENTINEL as PKG_SENTINEL,
    ParquetDeserializer as PkgDeserializer,
    convert_arrow_value as pkg_convert_arrow_value,
    read_file_as_rows as pkg_read_file_as_rows,
    register_parquet_handler as pkg_register,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_parquet_file(tmp_path: Path, data: dict[str, list[Any]], filename: str = "test.parquet") -> Path:
    """Write a small Parquet file with the given column data and return its path."""
    arrays = {col: pa.array(vals) for col, vals in data.items()}
    table = pa.table(arrays)
    path = tmp_path / filename
    pq.write_table(table, str(path))
    return path


def _make_struct_parquet(tmp_path: Path) -> Path:
    """Write a Parquet file with a struct column and a map column."""
    struct_type = pa.struct([("x", pa.int32()), ("y", pa.int32())])
    map_type = pa.map_(pa.string(), pa.int32())

    struct_array = pa.array(
        [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
        type=struct_type,
    )
    # pa.map_ requires list-of-pairs input
    map_array = pa.array(
        [[("a", 1)], [("b", 2)]],
        type=map_type,
    )
    scalar_array = pa.array([10, 20])

    table = pa.table(
        {"scalar": scalar_array, "nested": struct_array, "mapped": map_array}
    )
    path = tmp_path / "struct_test.parquet"
    pq.write_table(table, str(path))
    return path


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Reset the handler registry before and after every test."""
    _clear_handlers()
    yield
    _clear_handlers()


# ---------------------------------------------------------------------------
# 3.1 Constructor tests
# ---------------------------------------------------------------------------


def test_constructor_valid_path_succeeds(tmp_path: Path) -> None:
    """Scenario: Valid path constructs without error."""
    p = _make_parquet_file(tmp_path, {"id": [1, 2, 3], "label": ["a", "b", "c"]})
    ds = ParquetDeserializer(p)
    # Construction must not read any row data — num_rows is obtained from metadata.
    assert ds.num_rows == 3


def test_constructor_missing_path_raises(tmp_path: Path) -> None:
    """Scenario: Missing file raises FileNotFoundError."""
    missing = tmp_path / "does_not_exist.parquet"
    with pytest.raises(FileNotFoundError, match="does_not_exist.parquet"):
        ParquetDeserializer(missing)


def test_repr_contains_path_and_row_count(tmp_path: Path) -> None:
    """Scenario: repr shows path and row count (UI-P-002)."""
    p = _make_parquet_file(tmp_path, {"x": [1, 2, 3, 4, 5]})
    ds = ParquetDeserializer(p)
    r = repr(ds)
    assert str(p) in r
    assert "5" in r


# ---------------------------------------------------------------------------
# 3.2 Row index tests (TEST-P-001)
# ---------------------------------------------------------------------------


def test_positive_index_returns_correct_dict(tmp_path: Path) -> None:
    """Scenario: In-bounds positive index returns correct dict."""
    p = _make_parquet_file(tmp_path, {"id": [10, 20, 30], "val": [1.0, 2.0, 3.0]})
    ds = ParquetDeserializer(p)
    row = ds[1]
    assert isinstance(row, dict)
    assert row["id"] == 20
    assert row["val"] == pytest.approx(2.0)


def test_negative_index_python_semantics(tmp_path: Path) -> None:
    """Scenario: Negative index follows Python semantics."""
    p = _make_parquet_file(tmp_path, {"id": [10, 20, 30]})
    ds = ParquetDeserializer(p)
    row = ds[-1]
    assert row["id"] == 30


def test_out_of_bounds_index_raises_with_message(tmp_path: Path) -> None:
    """Scenario: Out-of-bounds index raises IndexError with message (TEST-P-001)."""
    p = _make_parquet_file(tmp_path, {"id": [1, 2, 3]})
    ds = ParquetDeserializer(p)
    with pytest.raises(IndexError, match="3"):
        ds[3]


def test_out_of_bounds_negative_index_raises(tmp_path: Path) -> None:
    """Scenario: Negative out-of-bounds index raises IndexError."""
    p = _make_parquet_file(tmp_path, {"id": [1, 2, 3]})
    ds = ParquetDeserializer(p)
    with pytest.raises(IndexError):
        ds[-4]


# ---------------------------------------------------------------------------
# 3.3 Slice tests (TEST-P-001)
# ---------------------------------------------------------------------------


def test_normal_slice_returns_correct_count(tmp_path: Path) -> None:
    """Scenario: Normal slice returns correct number of dicts."""
    p = _make_parquet_file(tmp_path, {"id": list(range(20))})
    ds = ParquetDeserializer(p)
    rows = ds[0:10]
    assert isinstance(rows, list)
    assert len(rows) == 10
    assert [r["id"] for r in rows] == list(range(10))


def test_clamped_slice_does_not_raise(tmp_path: Path) -> None:
    """Scenario: Clamped slice does not raise."""
    p = _make_parquet_file(tmp_path, {"id": list(range(5))})
    ds = ParquetDeserializer(p)
    rows = ds[0:10]
    assert len(rows) == 5


def test_stepped_slice_returns_correct_rows(tmp_path: Path) -> None:
    """Scenario: Stepped slice returns correct rows (every other row)."""
    p = _make_parquet_file(tmp_path, {"id": list(range(10))})
    ds = ParquetDeserializer(p)
    rows = ds[0:6:2]
    assert len(rows) == 3
    assert [r["id"] for r in rows] == [0, 2, 4]


def test_empty_slice_returns_empty_list(tmp_path: Path) -> None:
    """Scenario: Empty slice returns empty list."""
    p = _make_parquet_file(tmp_path, {"id": [1, 2, 3]})
    ds = ParquetDeserializer(p)
    rows = ds[5:5]
    assert rows == []


# ---------------------------------------------------------------------------
# 3.4 Field access tests on dict (TEST-P-002)
# ---------------------------------------------------------------------------


def test_field_access_known_column_returns_scalar(tmp_path: Path) -> None:
    """Scenario: Known column returns scalar."""
    p = _make_parquet_file(tmp_path, {"loss": [0.1, 0.2, 0.3]})
    ds = ParquetDeserializer(p)
    row = ds[0]
    assert "loss" in row
    assert row["loss"] == pytest.approx(0.1)


def test_field_access_unknown_column_raises_key_error(tmp_path: Path) -> None:
    """Scenario: Unknown column raises KeyError."""
    p = _make_parquet_file(tmp_path, {"loss": [0.1, 0.2]})
    ds = ParquetDeserializer(p)
    row = ds[0]
    with pytest.raises(KeyError, match="missing_column"):
        _ = row["missing_column"]


# ---------------------------------------------------------------------------
# 3.5 Field access on list-of-dicts (TEST-P-002)
# ---------------------------------------------------------------------------


def test_field_access_on_slice_maps_to_list(tmp_path: Path) -> None:
    """Scenario: Field access on a slice maps to list of scalars."""
    p = _make_parquet_file(tmp_path, {"label": ["cat", "dog", "bird"]})
    ds = ParquetDeserializer(p)
    rows = ds[0:3]
    labels = [r["label"] for r in rows]
    assert labels == ["cat", "dog", "bird"]


def test_field_access_on_list_missing_column_raises(tmp_path: Path) -> None:
    """Scenario: Unknown column on list raises KeyError on first miss."""
    p = _make_parquet_file(tmp_path, {"label": ["cat", "dog"]})
    ds = ParquetDeserializer(p)
    rows = ds[0:2]
    with pytest.raises(KeyError, match="missing"):
        _ = [r["missing"] for r in rows]


# ---------------------------------------------------------------------------
# 3.6 Chained traversal tests (TEST-P-003)
# ---------------------------------------------------------------------------


def test_index_then_field_returns_scalar(tmp_path: Path) -> None:
    """Scenario: Index then field returns scalar ([1].loss)."""
    p = _make_parquet_file(tmp_path, {"loss": [0.5, 0.6, 0.7]})
    ds = ParquetDeserializer(p)
    row = ds[1]
    result = row["loss"]
    assert result == pytest.approx(0.6)


def test_slice_then_field_returns_list_of_scalars(tmp_path: Path) -> None:
    """Scenario: Slice then field returns list of scalars ([0:5].label)."""
    p = _make_parquet_file(tmp_path, {"label": ["a", "b", "c", "d", "e"]})
    ds = ParquetDeserializer(p)
    rows = ds[0:5]
    result = [r["label"] for r in rows]
    assert result == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------------------
# 3.7 Opaque-type sentinel tests (TEST-P-004)
# ---------------------------------------------------------------------------


def test_struct_column_returns_dict(tmp_path: Path) -> None:
    """Scenario: pa.struct column returns Python dict (mirrors pa.Table.to_pydict)."""
    p = _make_struct_parquet(tmp_path)
    ds = ParquetDeserializer(p)
    row = ds[0]
    assert row["nested"] == {"x": 1, "y": 2}


def test_map_column_returns_sentinel(tmp_path: Path) -> None:
    """Scenario: pa.map_ column returns sentinel."""
    p = _make_struct_parquet(tmp_path)
    ds = ParquetDeserializer(p)
    row = ds[0]
    assert row["mapped"] == OPAQUE_SENTINEL


def test_scalar_column_not_affected_by_struct(tmp_path: Path) -> None:
    """Scalar column in same file as struct column still returns real value."""
    p = _make_struct_parquet(tmp_path)
    ds = ParquetDeserializer(p)
    row = ds[0]
    assert row["scalar"] == 10


# ---------------------------------------------------------------------------
# 3.8 Extension registry tests (TEST-P-005)
# ---------------------------------------------------------------------------


def test_registered_handler_is_called_instead_of_sentinel(tmp_path: Path) -> None:
    """Scenario: Registered handler is called instead of sentinel."""
    struct_type = pa.struct([("x", pa.int32())])
    called_with: list[Any] = []

    def _my_handler(value: Any, field: pa.Field) -> str:
        called_with.append((value, field))
        return "custom_result"

    register_parquet_handler(struct_type, _my_handler)

    table = pa.table({"nested": pa.array([{"x": 1}], type=struct_type)})
    p = tmp_path / "reg_test.parquet"
    pq.write_table(table, str(p))

    ds = ParquetDeserializer(p)
    row = ds[0]
    assert row["nested"] == "custom_result"
    assert len(called_with) == 1
    _, field = called_with[0]
    assert field.name == "nested"


def test_unregistered_type_still_returns_sentinel(tmp_path: Path) -> None:
    """Scenario: Handler registration does not affect unregistered types."""
    struct_type = pa.struct([("x", pa.int32())])
    map_type = pa.map_(pa.string(), pa.int32())

    register_parquet_handler(struct_type, lambda v, f: "handled_struct")

    table = pa.table({
        "nested": pa.array([{"x": 1}], type=struct_type),
        "mapped": pa.array([[("k", 1)]], type=map_type),
    })
    p = tmp_path / "mixed_types.parquet"
    pq.write_table(table, str(p))

    ds = ParquetDeserializer(p)
    row = ds[0]
    # struct was handled
    assert row["nested"] == "handled_struct"
    # map_ was NOT registered; should return sentinel
    assert row["mapped"] == OPAQUE_SENTINEL


def test_second_registration_replaces_first(tmp_path: Path) -> None:
    """Scenario: Registered handler replaces previous registration for the same type."""
    struct_type = pa.struct([("x", pa.int32())])

    register_parquet_handler(struct_type, lambda v, f: "first")
    register_parquet_handler(struct_type, lambda v, f: "second")

    table = pa.table({"nested": pa.array([{"x": 1}], type=struct_type)})
    p = tmp_path / "replace_test.parquet"
    pq.write_table(table, str(p))

    ds = ParquetDeserializer(p)
    row = ds[0]
    assert row["nested"] == "second"


# ---------------------------------------------------------------------------
# Package-level re-export tests
# ---------------------------------------------------------------------------


def test_package_exports_are_accessible() -> None:
    """Scenario: Public symbols importable from mlody.core.parquet."""
    assert PKG_SENTINEL == "<image>"
    assert PkgDeserializer is ParquetDeserializer
    assert pkg_register is register_parquet_handler


# ---------------------------------------------------------------------------
# Type-safety: non-int/slice key raises TypeError
# ---------------------------------------------------------------------------


def test_invalid_key_type_raises_type_error(tmp_path: Path) -> None:
    """Non-int, non-slice key raises TypeError."""
    p = _make_parquet_file(tmp_path, {"x": [1, 2, 3]})
    ds = ParquetDeserializer(p)
    with pytest.raises(TypeError, match="str"):
        ds["column_name"]  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# F10: convert_arrow_value public module-level function
# ---------------------------------------------------------------------------


class TestConvertArrowValueF10:
    """F10: convert_arrow_value extracted as a public module-level function.

    Covers the spec scenarios from specs/parquet-deserializer/spec.md.
    """

    def test_convert_arrow_value_importable_from_package(self) -> None:
        """Scenario: convert_arrow_value importable from mlody.core.parquet."""
        # pkg_convert_arrow_value was imported at module level from
        # mlody.core.parquet — this test asserts the import succeeded and the
        # function is the same object as the one in the deserializer module.
        assert pkg_convert_arrow_value is convert_arrow_value

    def test_registered_handler_invoked_by_convert_arrow_value(self) -> None:
        """Scenario: registered custom handler invoked by convert_arrow_value."""
        struct_type = pa.struct([("z", pa.int32())])
        calls: list[tuple[object, pa.Field]] = []

        def _custom(value: object, field: pa.Field) -> str:
            calls.append((value, field))
            return "from_handler"

        register_parquet_handler(struct_type, _custom)

        # Build a field with the registered type and call convert_arrow_value
        # directly (no Parquet file needed — just pass a scalar value).
        field = pa.field("z_col", struct_type)
        result = convert_arrow_value({"z": 99}, field)

        assert result == "from_handler"
        assert len(calls) == 1
        _, called_field = calls[0]
        assert called_field.name == "z_col"

    def test_convert_arrow_value_primitive_returns_python_scalar(self) -> None:
        """convert_arrow_value converts a pyarrow scalar to a Python primitive."""
        field = pa.field("n", pa.int64())
        raw = pa.array([42])[0]  # a pyarrow Int64Scalar
        result = convert_arrow_value(raw, field)
        assert result == 42
        assert isinstance(result, int)

    def test_convert_arrow_value_map_returns_sentinel(self) -> None:
        """convert_arrow_value returns OPAQUE_SENTINEL for map_ columns."""
        map_type = pa.map_(pa.string(), pa.int32())
        field = pa.field("m", map_type)
        result = convert_arrow_value("anything", field)
        assert result == OPAQUE_SENTINEL

    def test_deserializer_and_read_file_as_rows_identical_for_primitives(
        self, tmp_path: Path
    ) -> None:
        """Scenario: ParquetDeserializer and read_file_as_rows identical for primitives."""
        data: dict[str, list[Any]] = {
            "int_col": [1, 2, 3],
            "float_col": [1.1, 2.2, 3.3],
            "str_col": ["a", "b", "c"],
        }
        p = _make_parquet_file(tmp_path, data)

        ds = ParquetDeserializer(p)
        deserializer_rows = [ds[i] for i in range(3)]
        file_rows = pkg_read_file_as_rows(p)

        assert file_rows == deserializer_rows

    def test_deserializer_and_read_file_as_rows_identical_for_structs(
        self, tmp_path: Path
    ) -> None:
        """Scenario: ParquetDeserializer and read_file_as_rows identical for structs and maps."""
        p = _make_struct_parquet(tmp_path)

        ds = ParquetDeserializer(p)
        deserializer_rows = [ds[i] for i in range(2)]
        file_rows = pkg_read_file_as_rows(p)

        assert file_rows == deserializer_rows

    def test_deserializer_and_read_file_as_rows_identical_with_large_binary(
        self, tmp_path: Path
    ) -> None:
        """Scenario: identical output for large-binary column type."""
        large_binary_array = pa.array([b"hello", b"world"], type=pa.large_binary())
        table = pa.table({"blob": large_binary_array, "id": pa.array([1, 2])})
        p = tmp_path / "large_bin.parquet"
        pq.write_table(table, str(p))

        ds = ParquetDeserializer(p)
        deserializer_rows = [ds[i] for i in range(2)]
        file_rows = pkg_read_file_as_rows(p)

        # Both paths return OPAQUE_SENTINEL for large-binary columns.
        assert file_rows == deserializer_rows
        assert all(r["blob"] == OPAQUE_SENTINEL for r in file_rows)

    def test_deserializer_and_read_file_as_rows_identical_with_registered_handler(
        self, tmp_path: Path
    ) -> None:
        """Scenario: identical output when a custom handler is registered."""
        struct_type = pa.struct([("x", pa.int32())])
        register_parquet_handler(struct_type, lambda v, f: "handled")

        table = pa.table({"nested": pa.array([{"x": 1}, {"x": 2}], type=struct_type)})
        p = tmp_path / "handler_parity.parquet"
        pq.write_table(table, str(p))

        ds = ParquetDeserializer(p)
        deserializer_rows = [ds[i] for i in range(2)]
        file_rows = pkg_read_file_as_rows(p)

        assert file_rows == deserializer_rows
        assert all(r["nested"] == "handled" for r in file_rows)
