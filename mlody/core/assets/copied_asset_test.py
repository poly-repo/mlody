"""Tests for source-backed copied asset materialization."""

from __future__ import annotations

from pathlib import Path

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.local_asset import LocalPathAssetSource


def test_copied_asset_source_copies_upstream_file_and_records_lineage(tmp_path: Path) -> None:
    source_path = tmp_path / "source.csv"
    source_path.write_text("name,age\nAlice,30\n")
    destination_path = tmp_path / "cache" / "employees.csv"

    owner = Struct(
        kind="value",
        name="employees-local",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees-remote",
        _lineage=[],
        _source_value=Struct(
            kind="value",
            location=Struct(
                kind="location",
                type="remote",
                uri="https://example.com/employees.csv",
            ),
        ),
    )

    asset = CopiedAssetSource(
        value_name="employees-local",
        destination_path=str(destination_path),
        upstream_factory=lambda: LocalPathAssetSource(path=source_path),
        source_label=":employees-remote",
        lineage_owner=owner,
    )

    materialized = asset.materialize()

    assert destination_path.read_text() == source_path.read_text()
    assert materialized.path == destination_path
    assert [event.source for event in owner._lineage] == [
        "downloaded from",
        "copied from",
    ]
    assert owner._lineage[1].details["destination_path"] == str(destination_path)


def test_copied_asset_source_cache_hit_skips_upstream_factory(tmp_path: Path) -> None:
    destination_path = tmp_path / "cache" / "employees.csv"
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text("name,age\nAlice,30\n")

    owner = Struct(
        kind="value",
        name="employees-local",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees-remote",
        _lineage=[],
        _source_value=Struct(
            kind="value",
            location=Struct(
                kind="location",
                type="remote",
                uri="https://example.com/employees.csv",
            ),
        ),
    )

    def _unexpected_upstream() -> LocalPathAssetSource:
        raise AssertionError("upstream_factory should not run on cache hit")

    materialized = CopiedAssetSource(
        value_name="employees-local",
        destination_path=str(destination_path),
        upstream_factory=_unexpected_upstream,
        source_label=":employees-remote",
        lineage_owner=owner,
    ).materialize()

    assert materialized.path == destination_path
    assert materialized.content_hash is None
    assert [event.source for event in owner._lineage] == [
        "downloaded from",
        "copied from",
    ]


def test_copied_asset_source_raises_when_no_upstream_is_available(tmp_path: Path) -> None:
    destination_path = tmp_path / "cache" / "employees.csv"

    with pytest.raises(ValueError, match="cannot materialize source"):
        CopiedAssetSource(
            value_name="employees-local",
            destination_path=str(destination_path),
            upstream_factory=None,
            source_label=":employees-remote",
        ).materialize()
