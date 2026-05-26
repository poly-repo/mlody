"""Tests for mlody.db.evaluations — spec §Testing Strategy (evaluations)."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mlody.db.evaluations import open_db, write_evaluation

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MINIMAL_KWARGS: dict[str, object] = dict(
    username="testuser",
    hostname="testhost",
    requested_ref="main",
    resolved_sha="a" * 40,
    resolved_at="2026-03-27T12:00:00+00:00",
    repo="https://github.com/example/repo",
    local_only=False,
    value_description="bert-base-uncased fine-tuned on squad",
)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "mlody.sqlite")


# ---------------------------------------------------------------------------
# open_db tests
# ---------------------------------------------------------------------------


def test_open_db_creates_table(tmp_path: Path) -> None:
    """open_db creates the evaluations table — FR-002."""
    conn = _make_conn(tmp_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evaluations'"
        ).fetchone()
        assert row is not None
        assert row[0] == "evaluations"
    finally:
        conn.close()


def test_open_db_idempotent(tmp_path: Path) -> None:
    """Calling open_db twice on the same path does not raise — FR-002 idempotent."""
    conn1 = open_db(tmp_path / "mlody.sqlite")
    conn1.close()
    conn2 = open_db(tmp_path / "mlody.sqlite")
    conn2.close()


def test_open_db_wal_mode(tmp_path: Path) -> None:
    """open_db enables WAL journal mode — design D-8."""
    conn = _make_conn(tmp_path)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0] == "wal"
    finally:
        conn.close()


def test_open_db_file_permissions(tmp_path: Path) -> None:
    """DB file is created with 0600 permissions — NFR-SEC-001."""
    db_path = tmp_path / "mlody.sqlite"
    conn = open_db(db_path)
    conn.close()
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# write_evaluation tests
# ---------------------------------------------------------------------------


def test_write_evaluation_returns_uuid7(tmp_path: Path) -> None:
    """write_evaluation returns a valid UUID string with version 7 — FR-004."""
    conn = _make_conn(tmp_path)
    try:
        row_id = write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        parsed = uuid.UUID(row_id)
        # UUID v7: version nibble in bits 12-15 of clock_seq_hi_variant field
        # Python uuid stores version in .version attribute
        assert parsed.version == 7
    finally:
        conn.close()


def test_write_evaluation_row_count(tmp_path: Path) -> None:
    """One call to write_evaluation produces exactly one row — FR-003."""
    conn = _make_conn(tmp_path)
    try:
        write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()
        assert count is not None
        assert count[0] == 1
    finally:
        conn.close()


def test_write_evaluation_all_columns(tmp_path: Path) -> None:
    """All provided column values are stored exactly — FR-003."""
    conn = _make_conn(tmp_path)
    try:
        row_id = write_evaluation(
            conn,
            username="alice",
            hostname="devbox",
            requested_ref="v1.0.0",
            resolved_sha="b" * 40,
            resolved_at="2026-03-27T10:00:00+00:00",
            repo="https://github.com/example/omega",
            local_only=True,
            value_description="bert-large config",
            local_diff_sha="c" * 64,
            created_at="2026-03-27T10:00:01+00:00",
        )
        row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        (
            id_,
            created_at,
            completed_at,
            username,
            hostname,
            requested_ref,
            resolved_sha,
            resolved_at,
            repo,
            local_only,
            value_description,
            local_diff_sha,
            local_patch_sha,
        ) = row
        assert id_ == row_id
        assert created_at == "2026-03-27T10:00:01+00:00"
        assert completed_at is None
        assert username == "alice"
        assert hostname == "devbox"
        assert requested_ref == "v1.0.0"
        assert resolved_sha == "b" * 40
        assert resolved_at == "2026-03-27T10:00:00+00:00"
        assert repo == "https://github.com/example/omega"
        assert local_only == 1  # stored as INTEGER
        assert value_description == "bert-large config"
        assert local_diff_sha == "c" * 64
        assert local_patch_sha is None
    finally:
        conn.close()


def test_write_evaluation_twice_distinct_ids(tmp_path: Path) -> None:
    """Two inserts produce two rows with distinct, time-ordered IDs — FR-004."""
    conn = _make_conn(tmp_path)
    try:
        id1 = write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        id2 = write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        assert id1 != id2
        # UUID v7 strings are lexicographically time-ordered
        assert id2 > id1
    finally:
        conn.close()


def test_write_evaluation_empty_repo_convention(tmp_path: Path) -> None:
    """repo="" (empty string) stores "" — design D-6, Q1: NOT NULL with empty sentinel."""
    conn = _make_conn(tmp_path)
    try:
        kwargs = dict(_MINIMAL_KWARGS)
        kwargs["repo"] = ""
        row_id = write_evaluation(conn, **kwargs)  # type: ignore[arg-type]
        row = conn.execute(
            "SELECT repo FROM evaluations WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == ""
    finally:
        conn.close()


def test_write_evaluation_nullable_local_diff_sha(tmp_path: Path) -> None:
    """local_diff_sha=None stores NULL — spec column notes."""
    conn = _make_conn(tmp_path)
    try:
        row_id = write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        row = conn.execute(
            "SELECT local_diff_sha FROM evaluations WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        conn.close()


def test_write_evaluation_created_at_default(tmp_path: Path) -> None:
    """When created_at is omitted it defaults to a valid UTC ISO 8601 string — FR-003."""
    conn = _make_conn(tmp_path)
    try:
        row_id = write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
        row = conn.execute(
            "SELECT created_at FROM evaluations WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        created_at_str: str = row[0]
        # Must parse as a valid datetime; must contain UTC offset info
        parsed = datetime.fromisoformat(created_at_str)
        assert parsed.tzinfo is not None
    finally:
        conn.close()


def test_write_evaluation_empty_value_description_raises(tmp_path: Path) -> None:
    """Empty value_description raises ValueError before any DB write — spec §1.3."""
    conn = _make_conn(tmp_path)
    try:
        kwargs = dict(_MINIMAL_KWARGS)
        kwargs["value_description"] = ""
        with pytest.raises(ValueError, match="value_description"):
            write_evaluation(conn, **kwargs)  # type: ignore[arg-type]
        # Confirm no row was written
        count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()
        assert count is not None
        assert count[0] == 0
    finally:
        conn.close()


def test_write_evaluation_bad_resolved_sha_raises(tmp_path: Path) -> None:
    """resolved_sha not 40 hex chars raises ValueError — spec §1.3."""
    conn = _make_conn(tmp_path)
    try:
        kwargs = dict(_MINIMAL_KWARGS)
        kwargs["resolved_sha"] = "abc"
        with pytest.raises(ValueError, match="resolved_sha"):
            write_evaluation(conn, **kwargs)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_write_evaluation_json_files_untouched(tmp_path: Path) -> None:
    """Existing JSON files under ~/.cache/mlody/evaluations/ are not modified — FR-006."""
    # Simulate pre-existing JSON evaluation files in the conventional location
    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir()
    existing_json = eval_dir / "run-001.json"
    original_content = '{"result": "pass"}'
    existing_json.write_text(original_content)
    original_mtime = existing_json.stat().st_mtime

    # Write a DB evaluation (DB lives alongside the evaluations dir)
    conn = open_db(tmp_path / "mlody.sqlite")
    try:
        write_evaluation(conn, **_MINIMAL_KWARGS)  # type: ignore[arg-type]
    finally:
        conn.close()

    # JSON file must be byte-for-byte identical and untouched
    assert existing_json.read_text() == original_content
    assert existing_json.stat().st_mtime == original_mtime
