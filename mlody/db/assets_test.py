"""Tests for mlody.db.assets — external_assets, asset_blobs, asset_observations."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from mlody.db.assets import (
    ASSET_BLOBS_DDL,
    ASSET_OBSERVATIONS_DDL,
    EXTERNAL_ASSETS_DDL,
    LatestObservation,
    latest_observation,
    record_observation,
    upsert_blob,
    upsert_external_asset,
)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.sqlite")
    conn.execute(EXTERNAL_ASSETS_DDL)
    conn.execute(ASSET_BLOBS_DDL)
    conn.execute(ASSET_OBSERVATIONS_DDL)
    conn.commit()
    return conn


_BLOB_HASH = "a" * 64
_BLOB_PATH = "/cache/assets/http/abc/payload.csv"
_BLOB_SIZE = 1024

_ASSET_URI = "https://example.com/data.csv"
_ASSET_KEY = "sha256:" + "b" * 64


def _seed_blob(conn: sqlite3.Connection) -> None:
    upsert_blob(conn, content_hash=_BLOB_HASH, local_path=_BLOB_PATH, size_bytes=_BLOB_SIZE)


def _seed_asset(conn: sqlite3.Connection, **kwargs: object) -> str:
    return upsert_external_asset(
        conn,
        uri=_ASSET_URI,
        transport="http",
        cache_key=_ASSET_KEY,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# external_assets
# ---------------------------------------------------------------------------


class TestUpsertExternalAsset:
    def test_creates_row(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn)
        count = conn.execute("SELECT COUNT(*) FROM external_assets").fetchone()[0]
        assert count == 1

    def test_returns_uuid7(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        asset_id = _seed_asset(conn)
        assert uuid.UUID(asset_id).version == 7

    def test_same_id_on_duplicate_cache_key(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        id1 = _seed_asset(conn)
        id2 = _seed_asset(conn)
        assert id1 == id2

    def test_no_duplicate_rows_on_second_call(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn)
        _seed_asset(conn)
        count = conn.execute("SELECT COUNT(*) FROM external_assets").fetchone()[0]
        assert count == 1

    def test_updates_representation_on_second_call(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn, representation=None)
        _seed_asset(conn, representation="csv")
        row = conn.execute("SELECT representation FROM external_assets").fetchone()
        assert row[0] == "csv"

    def test_updates_freshness_on_second_call(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn, freshness_kind="unspecified")
        _seed_asset(conn, freshness_kind="ttl", freshness_max_age_seconds=3600)
        row = conn.execute(
            "SELECT freshness_kind, freshness_max_age_seconds FROM external_assets"
        ).fetchone()
        assert row[0] == "ttl"
        assert row[1] == 3600

    def test_value_name_coalesce_does_not_overwrite_with_none(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn, value_name="my_dataset")
        _seed_asset(conn, value_name=None)
        row = conn.execute("SELECT value_name FROM external_assets").fetchone()
        assert row[0] == "my_dataset"

    def test_value_name_can_be_updated_with_non_none(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn, value_name="old_name")
        _seed_asset(conn, value_name="new_name")
        row = conn.execute("SELECT value_name FROM external_assets").fetchone()
        assert row[0] == "new_name"

    def test_preserves_created_at_across_updates(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn)
        before = conn.execute("SELECT created_at FROM external_assets").fetchone()[0]
        _seed_asset(conn, representation="parquet")
        after = conn.execute("SELECT created_at FROM external_assets").fetchone()[0]
        assert before == after


# ---------------------------------------------------------------------------
# asset_blobs
# ---------------------------------------------------------------------------


class TestUpsertBlob:
    def test_creates_row(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        count = conn.execute("SELECT COUNT(*) FROM asset_blobs").fetchone()[0]
        assert count == 1

    def test_idempotent_on_same_hash(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        _seed_blob(conn)
        count = conn.execute("SELECT COUNT(*) FROM asset_blobs").fetchone()[0]
        assert count == 1

    def test_stores_path_and_size(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        row = conn.execute(
            "SELECT local_path, size_bytes FROM asset_blobs WHERE content_hash = ?",
            (_BLOB_HASH,),
        ).fetchone()
        assert row[0] == _BLOB_PATH
        assert row[1] == _BLOB_SIZE

    def test_two_distinct_hashes_create_two_rows(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        upsert_blob(conn, content_hash=_BLOB_HASH, local_path="/a", size_bytes=1)
        upsert_blob(conn, content_hash="c" * 64, local_path="/b", size_bytes=2)
        count = conn.execute("SELECT COUNT(*) FROM asset_blobs").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# asset_observations
# ---------------------------------------------------------------------------


class TestRecordObservation:
    def test_creates_row(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded")
        count = conn.execute("SELECT COUNT(*) FROM asset_observations").fetchone()[0]
        assert count == 1

    def test_returns_uuid7(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        obs_id = record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded")
        assert uuid.UUID(obs_id).version == 7

    def test_stores_all_optional_columns(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        obs_id = record_observation(
            conn,
            asset_id=asset_id,
            blob_sha=_BLOB_HASH,
            status="revalidated",
            etag='"abc123"',
            last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
            remote_digest="sha256:deadbeef",
            remote_digest_type="sha256",
            content_length=2048,
            update_time="2026-01-01T00:00:00Z",
            resolved_url="https://cdn.example.com/data.csv",
        )
        row = conn.execute(
            "SELECT status, etag, last_modified, remote_digest, remote_digest_type,"
            "       content_length, update_time, resolved_url"
            " FROM asset_observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row[0] == "revalidated"
        assert row[1] == '"abc123"'
        assert row[2] == "Thu, 01 Jan 2026 00:00:00 GMT"
        assert row[3] == "sha256:deadbeef"
        assert row[4] == "sha256"
        assert row[5] == 2048
        assert row[6] == "2026-01-01T00:00:00Z"
        assert row[7] == "https://cdn.example.com/data.csv"

    def test_multiple_observations_for_same_asset(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded")
        record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="revalidated")
        count = conn.execute("SELECT COUNT(*) FROM asset_observations").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# latest_observation
# ---------------------------------------------------------------------------


class TestLatestObservation:
    def test_returns_none_when_no_observations(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_asset(conn)
        result = latest_observation(conn, _ASSET_KEY)
        assert result is None

    def test_returns_none_for_unknown_cache_key(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        result = latest_observation(conn, "sha256:" + "z" * 64)
        assert result is None

    def test_returns_observation_after_download(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded")
        result = latest_observation(conn, _ASSET_KEY)
        assert isinstance(result, LatestObservation)
        assert result.blob_sha == _BLOB_HASH
        assert result.local_path == _BLOB_PATH
        assert result.size_bytes == _BLOB_SIZE
        assert result.status == "downloaded"

    def test_returns_most_recent_of_multiple_observations(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        _seed_blob(conn)
        asset_id = _seed_asset(conn)
        record_observation(
            conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded",
            etag='"first"',
        )
        record_observation(
            conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="revalidated",
            etag='"second"',
        )
        result = latest_observation(conn, _ASSET_KEY)
        assert result is not None
        assert result.status == "revalidated"
        assert result.etag == '"second"'

    def test_join_reflects_blob_fields(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        upsert_blob(conn, content_hash=_BLOB_HASH, local_path="/custom/path.parquet", size_bytes=99)
        asset_id = _seed_asset(conn)
        record_observation(conn, asset_id=asset_id, blob_sha=_BLOB_HASH, status="downloaded")
        result = latest_observation(conn, _ASSET_KEY)
        assert result is not None
        assert result.local_path == "/custom/path.parquet"
        assert result.size_bytes == 99
