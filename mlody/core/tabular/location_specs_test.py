"""Tests for typed location adapters and tabular-source factories."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from mlody.common.struct import Struct

from mlody.core.tabular import (
    CsvSource,
    DerivedLocationSpec,
    DerivedSource,
    MaterializedLocalSource,
    ParquetSource,
    PosixLocationSpec,
    RemoteLocationSpec,
    derived_location_spec_from_value,
    source_from_location,
    source_from_value,
)


def test_posix_location_spec_reads_direct_path_field() -> None:
    location = Struct(kind="location", type="path", path="/tmp/data.parquet")

    spec = PosixLocationSpec.from_location(location)

    assert spec == PosixLocationSpec(paths=("/tmp/data.parquet",), name="", kind="path")


def test_posix_location_spec_reads_path_from_attributes() -> None:
    location = Struct(
        kind="location",
        type="posix",
        attributes={"path": ["data/train.parquet", "data/test.parquet"]},
    )

    spec = PosixLocationSpec.from_location(location)

    assert spec == PosixLocationSpec(
        paths=("data/train.parquet", "data/test.parquet"),
        name="",
        kind="posix",
    )


def test_derived_location_spec_from_value_uses_source_location_paths() -> None:
    value_struct = Struct(
        kind="value",
        name="derived",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":upstream",
                "sql_fragment": "WHERE split = 'train'",
                "dialect": "duckdb",
                "output_path": "/tmp/output.parquet",
            },
        ),
        source=Struct(
            kind="value",
            location=Struct(kind="location", type="path", path="data/*.parquet"),
        ),
    )

    spec = derived_location_spec_from_value(value_struct)

    assert spec == DerivedLocationSpec(
        source_ref=":upstream",
        source_paths=("data/*.parquet",),
        query=spec.query,
        output_path=Path("/tmp/output.parquet"),
        name="derived",
    )
    assert spec is not None
    assert spec.query.sql == "WHERE split = 'train'"


def test_source_from_location_returns_parquet_source_for_plain_location() -> None:
    source = source_from_location(Struct(kind="location", type="path", path="data.parquet"))

    assert isinstance(source, ParquetSource)
    assert source.paths == ("data.parquet",)


def test_source_from_value_returns_derived_source_for_derived_value() -> None:
    value_struct = Struct(
        kind="value",
        name="derived",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":upstream",
                "source_paths": ["data/*.parquet"],
                "sql_fragment": "WHERE score > 0.5",
                "dialect": "duckdb",
                "output_path": "/tmp/output.parquet",
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, DerivedSource)
    assert source.spec.source_paths == ("data/*.parquet",)


def test_remote_location_spec_reads_uri_from_attributes() -> None:
    location = Struct(
        kind="location",
        type="remote",
        attributes={"uri": "https://example.com/data.csv"},
    )

    spec = RemoteLocationSpec.from_location(location)

    assert spec == RemoteLocationSpec(uri="https://example.com/data.csv", name="remote")


def test_source_from_value_returns_csv_source_for_posix_csv_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(kind="location", type="posix", path="data.csv"),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, CsvSource)
    assert source.paths == ("data.csv",)


def test_source_from_value_returns_materialized_local_source_for_source_backed_csv_value(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "staged.csv"
    staged_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    destination_path = tmp_path / "cache" / "employees.csv"
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
        mock_stage.return_value = Struct(
            uri="https://example.com/employees.csv",
            path=staged_path,
            content_hash="abc123",
        )
        source = source_from_value(value_struct)

        assert isinstance(source, MaterializedLocalSource)
        materialized = source.materialize()

    assert materialized == destination_path
    assert materialized.read_text() == staged_path.read_text()
    assert mock_stage.call_count == 1


def test_source_from_value_returns_remote_csv_source_for_remote_csv_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.csv"},
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator="|",
            header_required=False,
            multifile=False,
            attributes={
                "separator": "|",
                "header_required": False,
                "multifile": False,
            },
        ),
    )

    with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
        mock_stage.return_value = Struct(
            uri="https://example.com/data.csv",
            path=Path("/tmp/staged.csv"),
            content_hash="abc123",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, CsvSource)
    assert source.paths == ("/tmp/staged.csv",)
    assert source.separator == "|"
    assert source.header_required is False
    assert source.content_hash == "abc123"


def test_source_from_value_returns_remote_parquet_source_for_remote_parquet_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.parquet"},
        ),
        representation=Struct(
            kind="representation",
            name="parquet",
            multifile=False,
            attributes={"multifile": False},
        ),
    )

    with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
        mock_stage.return_value = Struct(
            uri="https://example.com/data.parquet",
            path=Path("/tmp/staged.parquet"),
            content_hash="def456",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, ParquetSource)
    assert source.paths == ("/tmp/staged.parquet",)
    assert source.content_hash == "def456"


def test_source_from_value_returns_none_for_unsupported_remote_representation() -> None:
    value_struct = Struct(
        kind="value",
        name="meta",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.json"},
        ),
        representation=Struct(
            kind="representation",
            name="json",
            attributes={},
        ),
    )

    assert source_from_value(value_struct) is None


def test_source_from_value_returns_none_for_remote_multifile_csv() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.csv"},
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=True,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": True,
            },
        ),
    )

    assert source_from_value(value_struct) is None


def test_source_from_value_builds_derived_source_for_remote_csv_source(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="high_paid",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":raw_employees",
                "sql_fragment": "WHERE salary > 100000",
                "dialect": "duckdb",
                "output_path": str(tmp_path / "derived.parquet"),
            },
        ),
        source=":raw_employees",
        _source_value=Struct(
            kind="value",
            name="raw_employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
    )

    with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
        mock_stage.return_value = Struct(
            uri="https://example.com/employees.csv",
            path=csv_path,
            content_hash="hash123",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, DerivedSource)
    materialized = source.materialize()
    assert materialized.exists()
    assert pq.read_table(materialized).column("name").to_pylist() == ["Alice"]


def test_source_backed_local_source_expands_home_and_reuses_cache(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "employees.csv"
    staged_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(
            kind="location",
            type="posix",
            path="~/.cache/mlody/artifacts/employees.csv",
        ),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with patch.dict("os.environ", {"HOME": str(tmp_path)}):
        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = Struct(
                uri="https://example.com/employees.csv",
                path=staged_path,
                content_hash="abc123",
            )
            source = source_from_value(value_struct)

            assert isinstance(source, MaterializedLocalSource)
            first = source.materialize()
            second = source.materialize()

    assert first == tmp_path / ".cache" / "mlody" / "artifacts" / "employees.csv"
    assert second == first
    assert first.read_text() == staged_path.read_text()
    assert mock_stage.call_count == 1


def test_source_backed_local_source_raises_for_non_tabular_source() -> None:
    destination_path = "/tmp/employees.csv"
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(kind="location", type="posix", path=destination_path),
        source=":meta",
        _source_value=Struct(
            kind="value",
            name="meta",
            location=Struct(kind="location", type="inline"),
            representation=Struct(kind="representation", name="json", attributes={}),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, MaterializedLocalSource)
    with pytest.raises(ValueError, match="non-tabular source 'meta'"):
        source.materialize()


def test_source_backed_local_source_raises_for_multiple_destination_paths() -> None:
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(
            kind="location",
            type="posix",
            path=["cache/one.csv", "cache/two.csv"],
        ),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with pytest.raises(ValueError, match="exactly one destination path"):
        source_from_value(value_struct)
