"""SQLite local_patches table — schema, DDL, and write path.

Stores compressed git patch content, deduplicated by SHA-256 of the raw patch.
Multiple evaluations may reference the same patch row via evaluations.local_patch_sha.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import zlib
from datetime import datetime, timezone
from typing import Final

_logger = logging.getLogger(__name__)

MAX_PATCH_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MB

LOCAL_PATCHES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS local_patches (
    sha        TEXT    PRIMARY KEY NOT NULL,
    created_at TEXT    NOT NULL,
    size_bytes INTEGER NOT NULL,
    content    BLOB    NOT NULL
);
"""


def write_local_patch(conn: sqlite3.Connection, patch: str) -> str | None:
    """Insert patch content if not already stored; return its SHA-256 or None.

    Returns None for empty patches.
    Returns None (and logs an error) when the uncompressed patch exceeds 10 MB.
    Deduplicates: when a row with the same SHA exists, skips the INSERT and
    returns the existing SHA so the evaluations FK still links correctly.
    """
    if not patch:
        return None
    raw = patch.encode()
    if len(raw) > MAX_PATCH_BYTES:
        _logger.error(
            "local patch too large (%d bytes > 10 MB); not storing in local_patches",
            len(raw),
        )
        return None
    sha = hashlib.sha256(raw).hexdigest()
    if conn.execute("SELECT sha FROM local_patches WHERE sha = ?", (sha,)).fetchone():
        return sha
    conn.execute(
        "INSERT INTO local_patches (sha, created_at, size_bytes, content) VALUES (?, ?, ?, ?)",
        (sha, datetime.now(timezone.utc).isoformat(), len(raw), zlib.compress(raw)),
    )
    conn.commit()
    return sha


def read_local_patch(conn: sqlite3.Connection, sha: str) -> str | None:
    """Return the decompressed patch for the given SHA, or None if not found."""
    row = conn.execute(
        "SELECT content FROM local_patches WHERE sha = ?", (sha,)
    ).fetchone()
    if row is None:
        return None
    return zlib.decompress(row[0]).decode()
