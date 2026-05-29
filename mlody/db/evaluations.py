"""SQLite evaluations database — schema, connection factory, and write path.

Schema notes:
- Future schema changes MUST use ALTER TABLE ADD COLUMN only.
  Dropping and recreating the table is forbidden (would destroy existing rows).
- All SQL uses ? placeholders; no string interpolation into SQL (NFR-SEC-002).
- repo is NOT NULL: use "" (empty string) when no origin remote is available
  (design D-6, Q1 answer: NOT NULL with empty-string sentinel).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import uuid_utils

EVALUATIONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS evaluations (
    id                TEXT    PRIMARY KEY NOT NULL,
    created_at        TEXT    NOT NULL,
    completed_at      TEXT,
    username          TEXT    NOT NULL,
    hostname          TEXT    NOT NULL,
    requested_ref     TEXT    NOT NULL,
    resolved_sha      TEXT    NOT NULL,
    resolved_at       TEXT    NOT NULL,
    repo              TEXT    NOT NULL,
    local_only        INTEGER NOT NULL,
    value_description TEXT    NOT NULL,
    local_diff_sha    TEXT
);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database at db_path and return a ready connection.

    Creates parent directories with mode 0o700 (user-only, following
    ensure_cache_root() precedent in mlody/resolver/cache.py — design D-10).
    Applies 0o600 file permissions, enables WAL mode, and runs the DDL
    idempotently before returning.
    """
    from mlody.db.assets import (  # noqa: PLC0415
        ASSET_BLOBS_DDL,
        ASSET_OBSERVATIONS_DDL,
        EXTERNAL_ASSETS_DDL,
    )
    from mlody.db.lineage_events import (  # noqa: PLC0415
        LINEAGE_EVENTS_DDL,
        LINEAGE_EVENTS_FINGERPRINT_IDX_DDL,
        LINEAGE_EVENTS_OWNER_IDX_DDL,
    )
    from mlody.db.local_patches import LOCAL_PATCHES_DDL  # noqa: PLC0415

    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Enforce owner-only read/write on the DB file (NFR-SEC-001).
    # Called unconditionally — idempotent on already-correct files.
    os.chmod(db_path, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(LOCAL_PATCHES_DDL)
    conn.execute(EXTERNAL_ASSETS_DDL)
    conn.execute(ASSET_BLOBS_DDL)
    conn.execute(ASSET_OBSERVATIONS_DDL)
    conn.execute(EVALUATIONS_DDL)
    conn.execute(LINEAGE_EVENTS_DDL)
    conn.execute(LINEAGE_EVENTS_OWNER_IDX_DDL)
    conn.execute(LINEAGE_EVENTS_FINGERPRINT_IDX_DDL)
    try:
        conn.execute(
            "ALTER TABLE evaluations ADD COLUMN local_patch_sha TEXT"
            " REFERENCES local_patches(sha)"
        )
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def write_evaluation(
    conn: sqlite3.Connection,
    *,
    username: str,
    hostname: str,
    requested_ref: str,
    resolved_sha: str,
    resolved_at: str,
    repo: str,
    local_only: bool,
    value_description: str,
    local_diff_sha: str | None = None,
    local_patch_sha: str | None = None,
    created_at: str | None = None,
) -> str:
    """Insert one evaluation row and return the UUID v7 primary key.

    Raises ValueError before the INSERT if:
    - resolved_sha is not exactly 40 hexadecimal characters.
    - value_description is an empty string.

    repo must be a str (NOT NULL column); pass "" when no origin remote exists.

    Does not catch DB exceptions — any SQLite error propagates to the caller
    so failures are never silent (NFR-AVAIL-001).
    """
    if len(resolved_sha) != 40 or not all(c in "0123456789abcdefABCDEF" for c in resolved_sha):
        raise ValueError(
            f"resolved_sha must be exactly 40 hexadecimal characters, got: {resolved_sha!r}"
        )
    if not value_description:
        raise ValueError("value_description must be a non-empty string")

    row_id = str(uuid_utils.uuid7())

    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO evaluations (
            id, created_at, completed_at,
            username, hostname,
            requested_ref, resolved_sha, resolved_at,
            repo, local_only, value_description, local_diff_sha, local_patch_sha
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            created_at,
            None,  # completed_at is always NULL on insert
            username,
            hostname,
            requested_ref,
            resolved_sha,
            resolved_at,
            repo,
            int(local_only),
            value_description,
            local_diff_sha,
            local_patch_sha,
        ),
    )
    conn.commit()
    return row_id
