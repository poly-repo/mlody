"""Tests for typed location adapters and tabular-source factories."""

from __future__ import annotations

from pathlib import Path

from mlody.common.struct import Struct

from mlody.core.tabular import (
    DerivedLocationSpec,
    DerivedSource,
    ParquetSource,
    PosixLocationSpec,
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
