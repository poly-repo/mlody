"""Tests for generic asset resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlody.common.struct import Struct
from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.http_asset import HttpAssetSource
from mlody.core.assets.local_asset import LocalAssetError, LocalPathAssetSource
from mlody.core.assets.resolution import asset_from_location, asset_from_value


def test_asset_from_location_returns_http_asset_for_remote_location() -> None:
    location = Struct(
        kind="location",
        type="remote",
        attributes={"uri": "https://example.com/data.json"},
    )

    asset = asset_from_location(location)

    assert isinstance(asset, HttpAssetSource)
    assert asset.uri == "https://example.com/data.json"


def test_asset_from_location_returns_local_asset_for_single_posix_path(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_text("{}")
    location = Struct(kind="location", type="posix", path=str(path))

    asset = asset_from_location(location)

    assert isinstance(asset, LocalPathAssetSource)
    assert asset.path == path


def test_asset_from_location_returns_none_for_globbed_posix_path() -> None:
    location = Struct(kind="location", type="posix", path="data/*.parquet")

    assert asset_from_location(location) is None


def test_asset_from_value_returns_http_asset_for_remote_non_tabular_value() -> None:
    value_struct = Struct(
        kind="value",
        location=Struct(kind="location", type="remote", uri="https://example.com/data.bin"),
        representation=Struct(kind="representation", name="json", attributes={}),
    )

    asset = asset_from_value(value_struct)

    assert isinstance(asset, HttpAssetSource)
    assert asset.uri == "https://example.com/data.bin"


def test_asset_from_value_returns_local_asset_for_plain_local_value(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hello: world\n")
    value_struct = Struct(
        kind="value",
        location=Struct(kind="location", type="posix", path=str(path)),
        representation=Struct(kind="representation", name="yaml", attributes={}),
    )

    asset = asset_from_value(value_struct)

    assert isinstance(asset, LocalPathAssetSource)
    materialized = asset.materialize()
    assert materialized.path == path
    assert materialized.metadata.transport == "posix"
    assert materialized.metadata.extra["path"] == str(path)


def test_asset_from_value_returns_copied_asset_for_source_backed_local_value(tmp_path: Path) -> None:
    source_path = tmp_path / "source.csv"
    source_path.write_text("name,age\nAlice,30\n")
    destination_path = tmp_path / "cached.csv"
    value_struct = Struct(
        kind="value",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        _source_value=Struct(
            kind="value",
            location=Struct(kind="location", type="posix", path=str(source_path)),
        ),
    )

    asset = asset_from_value(value_struct)

    assert isinstance(asset, CopiedAssetSource)
    materialized = asset.materialize()
    assert materialized.path == destination_path
    assert destination_path.read_text() == source_path.read_text()


def test_asset_from_value_returns_none_for_derived_value() -> None:
    value_struct = Struct(
        kind="value",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":upstream",
                "source_paths": ["data/*.parquet"],
                "sql_fragment": "WHERE split = 'train'",
                "dialect": "duckdb",
                "output_path": "/tmp/output.parquet",
            },
        ),
    )

    assert asset_from_value(value_struct) is None


def test_local_path_asset_source_raises_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    with pytest.raises(LocalAssetError, match="does not exist"):
        LocalPathAssetSource(path=missing).materialize()
