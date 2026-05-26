"""SQLite tables for external asset tracking — schema, DDL, and write path.

Three tables normalise what was previously stored in per-asset manifest.json
sidecar files:

  external_assets     — stable identity derived from the value declaration
  asset_observations  — one row per remote contact (download or revalidation)
  asset_blobs         — one row per unique content version, deduplicated by SHA-256

Schema notes (same constraints as evaluations.py):
- Future changes MUST use ALTER TABLE ADD COLUMN only.
- All SQL uses ? placeholders; no string interpolation (NFR-SEC-002).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

import uuid_utils

EXTERNAL_ASSETS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS external_assets (
    id                        TEXT    PRIMARY KEY NOT NULL,
    created_at                TEXT    NOT NULL,
    cache_key                 TEXT    NOT NULL UNIQUE,
    transport                 TEXT    NOT NULL,
    uri                       TEXT    NOT NULL,
    representation            TEXT,
    freshness_kind            TEXT    NOT NULL DEFAULT 'unspecified',
    freshness_max_age_seconds INTEGER,
    value_name                TEXT
);
"""

ASSET_BLOBS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS asset_blobs (
    content_hash TEXT    PRIMARY KEY NOT NULL,
    created_at   TEXT    NOT NULL,
    local_path   TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL
);
"""

ASSET_OBSERVATIONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS asset_observations (
    id                 TEXT    PRIMARY KEY NOT NULL,
    created_at         TEXT    NOT NULL,
    asset_id           TEXT    NOT NULL REFERENCES external_assets(id),
    blob_sha           TEXT    NOT NULL REFERENCES asset_blobs(content_hash),
    status             TEXT    NOT NULL,
    etag               TEXT,
    last_modified      TEXT,
    remote_digest      TEXT,
    remote_digest_type TEXT,
    content_length     INTEGER,
    update_time        TEXT,
    resolved_url       TEXT
);
"""


@dataclass(frozen=True, slots=True)
class LatestObservation:
    """Most recent remote contact for an asset."""

    observed_at: str
    status: str
    blob_sha: str
    local_path: str
    size_bytes: int
    etag: str | None
    remote_digest: str | None
    remote_digest_type: str | None
    content_length: int | None
    last_modified: str | None
    update_time: str | None
    resolved_url: str | None


def upsert_external_asset(
    conn: sqlite3.Connection,
    *,
    uri: str,
    transport: str,
    cache_key: str,
    representation: str | None = None,
    freshness_kind: str = "unspecified",
    freshness_max_age_seconds: int | None = None,
    value_name: str | None = None,
) -> str:
    """Insert or update an external asset row; return its id.

    cache_key is the stable unique identity (SHA-256 of URI). The row is
    created on first access; subsequent calls update the mutable
    declaration-derived fields (representation, freshness_*, value_name)
    while preserving id and created_at.

    value_name uses COALESCE so a None value_name never overwrites a
    previously recorded name.
    """
    new_id = str(uuid_utils.uuid7())
    now = _utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO external_assets
            (id, created_at, cache_key, transport, uri,
             representation, freshness_kind, freshness_max_age_seconds, value_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id, now, cache_key, transport, uri,
            representation, freshness_kind, freshness_max_age_seconds, value_name,
        ),
    )
    conn.execute(
        """
        UPDATE external_assets SET
            representation            = ?,
            freshness_kind            = ?,
            freshness_max_age_seconds = ?,
            value_name                = COALESCE(?, value_name)
        WHERE cache_key = ?
        """,
        (representation, freshness_kind, freshness_max_age_seconds, value_name, cache_key),
    )
    row = conn.execute(
        "SELECT id FROM external_assets WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    conn.commit()
    return str(row[0])


def upsert_blob(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    local_path: str,
    size_bytes: int,
) -> None:
    """Insert a blob row if one with this content_hash does not already exist.

    Idempotent: blob content is immutable so re-insertion with the same hash
    is always a no-op.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO asset_blobs (content_hash, created_at, local_path, size_bytes)
        VALUES (?, ?, ?, ?)
        """,
        (content_hash, _utc_now(), local_path, size_bytes),
    )
    conn.commit()


def record_observation(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    blob_sha: str,
    status: str,
    etag: str | None = None,
    last_modified: str | None = None,
    remote_digest: str | None = None,
    remote_digest_type: str | None = None,
    content_length: int | None = None,
    update_time: str | None = None,
    resolved_url: str | None = None,
) -> str:
    """Insert one observation row and return the UUID v7 primary key.

    status must be "downloaded" or "revalidated".
    blob_sha must reference an existing asset_blobs row.
    """
    row_id = str(uuid_utils.uuid7())
    conn.execute(
        """
        INSERT INTO asset_observations (
            id, created_at, asset_id, blob_sha, status,
            etag, last_modified, remote_digest, remote_digest_type,
            content_length, update_time, resolved_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, _utc_now(), asset_id, blob_sha, status,
            etag, last_modified, remote_digest, remote_digest_type,
            content_length, update_time, resolved_url,
        ),
    )
    conn.commit()
    return row_id


def latest_observation(
    conn: sqlite3.Connection,
    cache_key: str,
) -> LatestObservation | None:
    """Return the most recent observation for the asset identified by cache_key.

    Returns None when the asset has never been downloaded.
    Used by the freshness layer to decide whether to re-contact the remote.
    """
    row = conn.execute(
        """
        SELECT o.created_at, o.status, b.content_hash, b.local_path, b.size_bytes,
               o.etag, o.remote_digest, o.remote_digest_type, o.content_length,
               o.last_modified, o.update_time, o.resolved_url
        FROM   asset_observations o
        JOIN   asset_blobs        b ON o.blob_sha = b.content_hash
        WHERE  o.asset_id = (SELECT id FROM external_assets WHERE cache_key = ?)
        ORDER  BY o.created_at DESC
        LIMIT  1
        """,
        (cache_key,),
    ).fetchone()
    if row is None:
        return None
    return LatestObservation(
        observed_at=str(row[0]),
        status=str(row[1]),
        blob_sha=str(row[2]),
        local_path=str(row[3]),
        size_bytes=int(row[4]),
        etag=row[5],
        remote_digest=row[6],
        remote_digest_type=row[7],
        content_length=row[8],
        last_modified=row[9],
        update_time=row[10],
        resolved_url=row[11],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
