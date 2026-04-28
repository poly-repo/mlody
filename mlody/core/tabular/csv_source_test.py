"""Tests for the CSV-backed tabular source abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlody.core.tabular.csv_source import CsvSource


def test_preview_returns_limited_rows_and_total_count(tmp_path: Path) -> None:
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text("name,age\nAlice,30\nBob,40\nCara,50\n")

    source = CsvSource(paths=(str(csv_path),))
    preview = source.preview(2)

    assert preview.total_rows == 3
    assert preview.table.num_rows == 2
    assert preview.table.column_names == ["name", "age"]


def test_count_unions_multiple_csv_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "dataset"
    data_dir.mkdir()
    (data_dir / "part-000.csv").write_text("name,age\nAlice,30\nBob,40\n")
    (data_dir / "part-001.csv").write_text("name,age\nCara,50\n")

    source = CsvSource(paths=(str(data_dir / "*.csv"),))

    assert source.count() == 3


def test_separator_is_honored(tmp_path: Path) -> None:
    csv_path = tmp_path / "employees.psv"
    csv_path.write_text("name|age\nAlice|30\nBob|40\n")

    source = CsvSource(paths=(str(csv_path),), separator="|")
    preview = source.preview(10)

    assert preview.table.column_names == ["name", "age"]
    assert preview.total_rows == 2


def test_header_required_false_autogenerates_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "headerless.csv"
    csv_path.write_text("Alice,30\nBob,40\n")

    source = CsvSource(paths=(str(csv_path),), header_required=False)
    preview = source.preview(10)

    assert preview.total_rows == 2
    assert len(preview.table.column_names) == 2


def test_materialize_raises_for_missing_source(tmp_path: Path) -> None:
    source = CsvSource(paths=(str(tmp_path / "missing.csv"),))

    with pytest.raises(FileNotFoundError):
        source.materialize()
