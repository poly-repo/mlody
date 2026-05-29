"""SQLite persistence helpers for mlody lineage events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import uuid_utils
except ModuleNotFoundError:  # pragma: no cover - exercised only outside Bazel deps.
    class _UuidUtilsCompat:
        @staticmethod
        def uuid7() -> uuid.UUID:
            return uuid.uuid4()

    uuid_utils = _UuidUtilsCompat()

from mlody.common.struct import Struct, struct_like_to_struct

LINEAGE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS lineage_events (
    id TEXT PRIMARY KEY NOT NULL,
    created_at TEXT NOT NULL,
    owner_label TEXT NOT NULL,
    owner_hash TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    event_payload TEXT NOT NULL
)
"""

LINEAGE_EVENTS_OWNER_IDX_DDL = """
CREATE INDEX IF NOT EXISTS lineage_events_owner_idx
ON lineage_events (owner_label, owner_hash, created_at, id)
"""

LINEAGE_EVENTS_FINGERPRINT_IDX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS lineage_events_owner_fingerprint_idx
ON lineage_events (owner_label, owner_hash, event_fingerprint)
"""


def open_lineage_db(db_path: Path) -> sqlite3.Connection:
    """Open the shared SQLite DB path with lineage tables ready."""

    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    os.chmod(db_path, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(LINEAGE_EVENTS_DDL)
    conn.execute(LINEAGE_EVENTS_OWNER_IDX_DDL)
    conn.execute(LINEAGE_EVENTS_FINGERPRINT_IDX_DDL)
    conn.commit()
    return conn


def write_lineage_event(
    conn: sqlite3.Connection,
    *,
    owner_label: str,
    owner_hash: str,
    event: object,
) -> bool:
    """Insert one lineage row when this owner does not already have the event."""

    payload = _serialize_lineage_event(event)
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO lineage_events (
            id,
            created_at,
            owner_label,
            owner_hash,
            event_fingerprint,
            event_payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid_utils.uuid7()),
            datetime.now(timezone.utc).isoformat(),
            owner_label,
            owner_hash,
            fingerprint,
            payload,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def read_lineage_events(
    conn: sqlite3.Connection,
    *,
    owner_label: str,
    owner_hash: str,
) -> list[object]:
    """Return lineage events stored for one owner identity in write order."""

    rows = conn.execute(
        """
        SELECT event_payload
        FROM lineage_events
        WHERE owner_label = ? AND owner_hash = ?
        ORDER BY created_at, id
        """,
        (owner_label, owner_hash),
    ).fetchall()
    return [_deserialize_lineage_event(row[0]) for row in rows]


def _serialize_lineage_event(event: object) -> str:
    payload = {
        "accessor": getattr(event, "accessor", None),
        "new_value": _json_payload(getattr(event, "new_value", None)),
        "source": getattr(event, "source", None),
        "reason": getattr(event, "reason", None),
        "timestamp": getattr(event, "timestamp", None),
        "mode": getattr(event, "mode", None),
        "details": _json_payload(getattr(event, "details", None)),
        "kind": getattr(event, "kind", "lineage_event"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _deserialize_lineage_event(payload: str) -> object:
    decoded = json.loads(payload)
    return Struct(
        kind=str(decoded.get("kind", "lineage_event")),
        accessor=str(decoded["accessor"]),
        new_value=_restore_struct_value(decoded.get("new_value")),
        source=decoded.get("source"),
        reason=decoded.get("reason"),
        timestamp=decoded.get("timestamp"),
        mode=str(decoded["mode"]),
        details=decoded.get("details"),
    )


def _json_payload(value: object) -> object:
    if callable(value):
        return None
    normalized = struct_like_to_struct(value)
    if isinstance(normalized, Struct):
        return {
            str(name): _json_payload(child)
            for name, child in normalized.as_mapping().items()
            if not str(name).startswith("_")
        }
    if isinstance(normalized, dict):
        return {
            str(name): _json_payload(child)
            for name, child in normalized.items()
            if not str(name).startswith("_")
        }
    if isinstance(normalized, list):
        return [_json_payload(child) for child in normalized]
    if isinstance(normalized, tuple):
        return [_json_payload(child) for child in normalized]
    return normalized


def _restore_struct_value(value: object) -> object:
    if isinstance(value, dict):
        return Struct(
            **{
                str(name): _restore_struct_value(child)
                for name, child in value.items()
            },
        )
    if isinstance(value, list):
        return [_restore_struct_value(child) for child in value]
    return value
