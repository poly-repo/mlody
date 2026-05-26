"""Tests for the persistent HTTP asset cache."""

from __future__ import annotations

import functools
import http.server
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mlody.common.struct import Struct
from mlody.core.assets.http_asset import HttpAssetError, HttpAssetSource
from mlody.core.assets.manifest import load_manifest
from mlody.db.assets import (
    ASSET_BLOBS_DDL,
    ASSET_OBSERVATIONS_DDL,
    EXTERNAL_ASSETS_DDL,
    latest_observation,
    upsert_external_asset,
)


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


def test_http_asset_source_preserves_remote_suffix(http_server: tuple[str, Path], tmp_path: Path) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.parquet"
    source_path.write_text("parquet-bytes")

    materialized = HttpAssetSource(
        uri=f"{base_url}/employees.parquet",
        cache_root=cache_root,
    ).materialize()

    assert materialized.path.suffix == ".parquet"


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


def test_http_asset_source_ttl_reuses_recent_cache_without_revalidation(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")
    uri = f"{base_url}/employees.csv"
    freshness = Struct(
        kind="freshness",
        type="ttl",
        name="ttl",
        attributes={"duration": "P1D"},
    )

    first = HttpAssetSource(uri=uri, cache_root=cache_root, freshness=freshness).materialize()
    source_path.unlink()

    with patch("mlody.core.assets.http_asset.fetch_http_info") as mock_http_info:
        second = HttpAssetSource(
            uri=uri,
            cache_root=cache_root,
            freshness=freshness,
        ).materialize()

    assert first.path == second.path
    assert first.content_hash == second.content_hash
    mock_http_info.assert_not_called()


def test_http_asset_source_always_revalidates_and_redownloads_when_remote_changes(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")
    uri = f"{base_url}/employees.csv"
    freshness = Struct(
        kind="freshness",
        type="always",
        name="always",
        attributes={},
    )

    first = HttpAssetSource(uri=uri, cache_root=cache_root, freshness=freshness).materialize()
    source_path.write_text("name,age\nAlice,30\nBob,40\nCarol,50\n")
    second = HttpAssetSource(uri=uri, cache_root=cache_root, freshness=freshness).materialize()

    assert first.path == second.path
    assert first.content_hash != second.content_hash
    assert second.path.read_text() == source_path.read_text()


def test_http_asset_source_rejects_unsupported_scheme(tmp_path: Path) -> None:
    with pytest.raises(HttpAssetError, match="http/https"):
        HttpAssetSource(uri="file:///tmp/data.csv", cache_root=tmp_path / "assets-cache").materialize()


def test_http_asset_source_logs_uri_access(
    http_server: tuple[str, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    base_url, root = http_server
    cache_root = tmp_path / "assets-cache"
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")
    uri = f"{base_url}/employees.csv"

    with caplog.at_level("INFO", logger="mlody.core.assets.http_asset"):
        HttpAssetSource(uri=uri, cache_root=cache_root).materialize()

    assert any(
        "Fetching remote URI" in record.message and uri in record.message
        for record in caplog.records
    )
    assert any(
        "Staged remote URI" in record.message and uri in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# DB-backed path tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def asset_db(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh in-memory SQLite connection with the three asset DDLs."""
    conn = sqlite3.connect(tmp_path / "test_assets.sqlite")
    conn.execute(EXTERNAL_ASSETS_DDL)
    conn.execute(ASSET_BLOBS_DDL)
    conn.execute(ASSET_OBSERVATIONS_DDL)
    conn.commit()
    return conn


def _seed_asset(conn: sqlite3.Connection, uri: str) -> str:
    from mlody.core.assets.manifest import cache_key_for_uri

    return upsert_external_asset(
        conn,
        uri=uri,
        transport="http",
        cache_key=cache_key_for_uri(uri),
    )


def test_db_download_inserts_blob_and_observation(
    http_server: tuple[str, Path],
    tmp_path: Path,
    asset_db: sqlite3.Connection,
) -> None:
    base_url, root = http_server
    (root / "data.csv").write_text("a,b\n1,2\n")
    uri = f"{base_url}/data.csv"
    asset_id = _seed_asset(asset_db, uri)

    materialized = HttpAssetSource(
        uri=uri,
        cache_root=tmp_path / "cache",
        db_conn=asset_db,
        asset_id=asset_id,
    ).materialize()

    blob_row = asset_db.execute(
        "SELECT local_path, size_bytes FROM asset_blobs WHERE content_hash = ?",
        (materialized.content_hash,),
    ).fetchone()
    assert blob_row is not None
    assert blob_row[0] == str(materialized.path)

    obs_count = asset_db.execute(
        "SELECT COUNT(*) FROM asset_observations WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()[0]
    assert obs_count == 1

    obs_row = asset_db.execute(
        "SELECT status FROM asset_observations WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    assert obs_row[0] == "downloaded"


def test_db_cache_hit_does_not_add_observation(
    http_server: tuple[str, Path],
    tmp_path: Path,
    asset_db: sqlite3.Connection,
) -> None:
    base_url, root = http_server
    (root / "data.csv").write_text("a,b\n1,2\n")
    uri = f"{base_url}/data.csv"
    asset_id = _seed_asset(asset_db, uri)
    freshness = Struct(kind="freshness", type="ttl", name="ttl", attributes={"duration": "P1D"})

    source = HttpAssetSource(
        uri=uri,
        cache_root=tmp_path / "cache",
        db_conn=asset_db,
        asset_id=asset_id,
        freshness=freshness,
    )
    source.materialize()
    (root / "data.csv").unlink()

    with patch("mlody.core.assets.http_asset.fetch_http_info") as mock_fetch:
        source.materialize()

    mock_fetch.assert_not_called()
    obs_count = asset_db.execute(
        "SELECT COUNT(*) FROM asset_observations WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()[0]
    assert obs_count == 1


def test_db_revalidation_adds_revalidated_observation(
    http_server: tuple[str, Path],
    tmp_path: Path,
    asset_db: sqlite3.Connection,
) -> None:
    base_url, root = http_server
    (root / "data.csv").write_text("a,b\n1,2\n")
    uri = f"{base_url}/data.csv"
    asset_id = _seed_asset(asset_db, uri)
    freshness = Struct(kind="freshness", type="always", name="always", attributes={})

    source = HttpAssetSource(
        uri=uri,
        cache_root=tmp_path / "cache",
        db_conn=asset_db,
        asset_id=asset_id,
        freshness=freshness,
    )
    first = source.materialize()
    # File unchanged — same content-length on second HEAD → no change → revalidated
    second = source.materialize()

    assert first.content_hash == second.content_hash
    obs_rows = asset_db.execute(
        "SELECT status FROM asset_observations WHERE asset_id = ? ORDER BY created_at",
        (asset_id,),
    ).fetchall()
    assert len(obs_rows) == 2
    assert obs_rows[0][0] == "downloaded"
    assert obs_rows[1][0] == "revalidated"


def test_db_revalidation_redownloads_when_content_changes(
    http_server: tuple[str, Path],
    tmp_path: Path,
    asset_db: sqlite3.Connection,
) -> None:
    base_url, root = http_server
    csv_path = root / "data.csv"
    csv_path.write_text("a,b\n1,2\n")
    uri = f"{base_url}/data.csv"
    asset_id = _seed_asset(asset_db, uri)
    freshness = Struct(kind="freshness", type="always", name="always", attributes={})

    source = HttpAssetSource(
        uri=uri,
        cache_root=tmp_path / "cache",
        db_conn=asset_db,
        asset_id=asset_id,
        freshness=freshness,
    )
    first = source.materialize()

    csv_path.write_text("a,b\n1,2\n3,4\n")
    second = source.materialize()

    assert first.content_hash != second.content_hash
    obs_rows = asset_db.execute(
        "SELECT status FROM asset_observations WHERE asset_id = ? ORDER BY created_at",
        (asset_id,),
    ).fetchall()
    assert len(obs_rows) == 2
    assert obs_rows[1][0] == "downloaded"


def test_db_latest_observation_returns_none_before_first_download(
    asset_db: sqlite3.Connection,
) -> None:
    from mlody.core.assets.manifest import cache_key_for_uri

    obs = latest_observation(asset_db, cache_key_for_uri("http://example.com/never.csv"))
    assert obs is None
