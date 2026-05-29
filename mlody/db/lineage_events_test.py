"""Tests for SQLite-backed lineage event persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from common.python.starlarkish.core.struct import Struct

from mlody.core.lineage import build_lineage_event
from mlody.db.lineage_events import open_lineage_db, read_lineage_events, write_lineage_event


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    return open_lineage_db(tmp_path / "mlody.sqlite")


def test_open_db_creates_lineage_events_table(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage_events'"
        ).fetchone()
        assert row is not None
        assert row[0] == "lineage_events"
    finally:
        conn.close()


def test_write_and_read_lineage_event_round_trip(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    try:
        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data="https://example.com/data.csv"),
            source="downloaded from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "remote-download",
                "content_hash": "abc123",
                "staged_path": "/tmp/staged.csv",
            },
        )

        recorded = write_lineage_event(
            conn,
            owner_label="@demo//assets:employees",
            owner_hash="abc123",
            event=event,
        )
        round_tripped = read_lineage_events(
            conn,
            owner_label="@demo//assets:employees",
            owner_hash="abc123",
        )

        assert recorded is True
        assert len(round_tripped) == 1
        assert round_tripped[0].source == "downloaded from"
        assert round_tripped[0].new_value.data == "https://example.com/data.csv"
        assert round_tripped[0].details["content_hash"] == "abc123"
    finally:
        conn.close()


def test_write_lineage_event_dedupes_identical_payloads(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    try:
        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data=":employees"),
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "local-copy",
                "content_hash": "def456",
                "destination_path": "/tmp/employees.csv",
            },
        )

        first = write_lineage_event(
            conn,
            owner_label="@demo//assets:employees_local",
            owner_hash="def456",
            event=event,
        )
        second = write_lineage_event(
            conn,
            owner_label="@demo//assets:employees_local",
            owner_hash="def456",
            event=event,
        )
        count = conn.execute("SELECT COUNT(*) FROM lineage_events").fetchone()

        assert first is True
        assert second is False
        assert count is not None
        assert count[0] == 1
    finally:
        conn.close()
