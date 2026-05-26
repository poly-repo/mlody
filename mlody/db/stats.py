"""Database statistics — shared between the CLI and the stage HTTP server."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def gather_stats(conn: sqlite3.Connection, db_path: Path) -> dict[str, object]:
    """Return global + per-table statistics as a serialisable dict."""
    table_names: list[str] = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]

    tables: dict[str, object] = {}
    total_rows = 0
    for name in table_names:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
        ts: dict[str, object] = {}

        row_count: int = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        ts["rows"] = row_count
        total_rows += row_count

        if "created_at" in cols:
            r = conn.execute(
                f"SELECT MIN(created_at), MAX(created_at) FROM {name}"
            ).fetchone()
            ts["oldest"] = r[0]
            ts["newest"] = r[1]

        if "content" in cols and "size_bytes" in cols:
            r = conn.execute(
                f"SELECT SUM(size_bytes), SUM(LENGTH(content)) FROM {name}"
            ).fetchone()
            ts["uncompressed_bytes"] = r[0] or 0
            ts["compressed_bytes"] = r[1] or 0

        for col in cols:
            if col.endswith("_sha") and col != "sha":
                n: int = conn.execute(
                    f"SELECT COUNT(*) FROM {name} WHERE {col} IS NOT NULL"
                ).fetchone()[0]
                ts[f"with_{col}"] = n

        tables[name] = ts

    db_size = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    return {
        "db_path": str(db_path),
        "db_size": db_size,
        "wal_size": wal_size,
        "total_rows": total_rows,
        "tables": tables,
    }


def clear_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """DELETE all rows from every table; return {table_name: deleted_count}."""
    table_names: list[str] = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    deleted: dict[str, int] = {}
    for name in table_names:
        before: int = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        conn.execute(f"DELETE FROM {name}")
        deleted[name] = before
    conn.commit()
    return deleted
