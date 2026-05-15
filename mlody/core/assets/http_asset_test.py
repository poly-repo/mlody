"""Tests for the persistent HTTP asset cache."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from mlody.core.assets.http_asset import HttpAssetError, HttpAssetSource
from mlody.core.assets.manifest import load_manifest


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


def test_http_asset_source_downloads_and_writes_manifest(http_server: tuple[str, Path], tmp_path: Path) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")

    materialized = HttpAssetSource(
        uri=f"{base_url}/employees.csv",
        cache_root=cache_root,
    ).materialize()

    manifest = load_manifest(materialized.path.parent / "manifest.json")
    assert materialized.path.exists()
    assert materialized.path.suffix == ".csv"
    assert materialized.content_hash == manifest.local.content_hash
    assert manifest.request.uri == f"{base_url}/employees.csv"
    assert manifest.local.payload_relpath == materialized.path.name


def test_http_asset_source_reuses_cached_payload_across_instances(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")
    uri = f"{base_url}/employees.csv"

    first = HttpAssetSource(uri=uri, cache_root=cache_root).materialize()
    source_path.unlink()
    second = HttpAssetSource(uri=uri, cache_root=cache_root).materialize()

    assert first.path == second.path
    assert first.content_hash == second.content_hash
    assert second.path.exists()


def test_http_asset_source_rejects_unsupported_scheme(tmp_path: Path) -> None:
    with pytest.raises(HttpAssetError, match="http/https"):
        HttpAssetSource(uri="file:///tmp/data.csv", cache_root=tmp_path / "assets-cache").materialize()
