"""Tests for generic asset resolution."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mlody.common.struct import Struct
from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.http_asset import HttpAssetSource
from mlody.core.assets.local_asset import LocalAssetError, LocalPathAssetSource
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.assets.resolution import asset_from_location, asset_from_value


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    """Serve *tmp_path* over HTTP and return ``(base_url, root)``."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", tmp_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _remote_asset(path: Path, *, uri: str, content_hash: str) -> MaterializedAsset:
    return MaterializedAsset(
        path=path,
        content_hash=content_hash,
        metadata=AssetMetadata(
            uri=uri,
            resolved_url=uri,
            digest=None,
            digest_type=None,
            length=None,
            update_time=None,
            transport="http",
        ),
    )


def _manual() -> Struct:
    return Struct(kind="freshness", type="manual", name="manual", attributes={})


def _always() -> Struct:
    return Struct(kind="freshness", type="always", name="always", attributes={})


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
        _lineage=[],
        location=Struct(kind="location", type="remote", uri="https://example.com/data.bin"),
        representation=Struct(kind="representation", name="json", attributes={}),
    )

    asset = asset_from_value(value_struct)

    assert asset is not None
    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(
            Path("/tmp/data.bin"),
            uri="https://example.com/data.bin",
            content_hash="abc123",
        )
        materialized = asset.materialize()

    assert materialized.path == Path("/tmp/data.bin")
    assert value_struct._lineage[0].source == "downloaded from"
    assert value_struct._lineage[0].details["staged_path"] == "/tmp/data.bin"


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


def test_asset_from_value_copies_source_backed_local_html_value(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    source_path = root / "page.html"
    source_path.write_text("<html><body><h1>Hello</h1></body></html>")
    destination_path = tmp_path / "cached.html"
    value_struct = Struct(
        kind="value",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        representation=Struct(kind="representation", name="html", attributes={}),
        _source_value=Struct(
            kind="value",
            location=Struct(
                kind="location",
                type="remote",
                uri=f"{base_url}/page.html",
            ),
            representation=Struct(kind="representation", name="html", attributes={}),
        ),
    )

    asset = asset_from_value(value_struct)

    assert isinstance(asset, CopiedAssetSource)
    materialized = asset.materialize()
    assert materialized.path == destination_path
    assert destination_path.read_text() == source_path.read_text()


def test_asset_from_value_uses_downstream_freshness_for_cached_remote_value(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    source_path = root / "source.csv"
    source_path.write_text("name,age\nAlice,30\n")
    destination_path = tmp_path / "cached.csv"
    value_struct = Struct(
        kind="value",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        freshness=_always(),
        _source_value=Struct(
            kind="value",
            freshness=_manual(),
            location=Struct(
                kind="location",
                type="remote",
                uri=f"{base_url}/source.csv",
            ),
        ),
    )

    asset = asset_from_value(value_struct)

    assert isinstance(asset, CopiedAssetSource)
    first = asset.materialize()
    source_path.write_text("name,age\nAlice,30\nBob,40\n")
    second = asset.materialize()

    assert first.path == second.path
    assert destination_path.read_text() == source_path.read_text()
    assert first.content_hash != second.content_hash


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
