"""CLI `db` subcommand group for inspecting the mlody SQLite database."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mlody.cli.main import cli
from mlody.cli.show import _DEFAULT_CACHE_SUFFIX, _DEFAULT_DB_NAME
from mlody.db.evaluations import open_db
from mlody.db.stats import clear_tables, gather_stats


@cli.group()
def db() -> None:
    """Inspect the mlody SQLite database."""


@db.command("status")
def db_status() -> None:
    """Show statistics about the mlody database."""
    db_path = Path.home() / _DEFAULT_CACHE_SUFFIX / _DEFAULT_DB_NAME
    if not db_path.exists():
        click.echo(f"No database found at {db_path}")
        return

    conn = open_db(db_path)
    try:
        stats = gather_stats(conn, db_path)
    finally:
        conn.close()

    _render_status(stats)


@db.command("clear")
@click.confirmation_option(prompt="Delete all rows from all tables?")
def db_clear() -> None:
    """Delete all rows from every table in the mlody database."""
    db_path = Path.home() / _DEFAULT_CACHE_SUFFIX / _DEFAULT_DB_NAME
    if not db_path.exists():
        click.echo(f"No database found at {db_path}")
        return

    conn = open_db(db_path)
    try:
        deleted = clear_tables(conn)
    finally:
        conn.close()

    for table_name, count in deleted.items():
        click.echo(f"  {table_name}: deleted {count} row{'s' if count != 1 else ''}")
    click.echo("Done.")


def _fmt_bytes(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    for k, v in rows:
        t.add_row(k, v)
    return t


def _render_status(stats: dict[str, object]) -> None:
    console = Console()
    wal_size = stats.get("wal_size") or 0
    global_rows: list[tuple[str, str]] = [
        ("DB path", str(stats["db_path"])),
        ("DB size", _fmt_bytes(int(stats["db_size"]))),  # type: ignore[arg-type]
        ("WAL size", _fmt_bytes(int(wal_size)) if wal_size else "—"),
        ("Total rows", str(stats["total_rows"])),
    ]
    console.print(Panel(_kv_table(global_rows), title="Global"))

    tables = stats.get("tables") or {}
    for table_name, ts_raw in tables.items():  # type: ignore[union-attr]
        ts: dict[str, object] = ts_raw  # type: ignore[assignment]
        row_count = int(ts["rows"])  # type: ignore[arg-type]
        table_rows: list[tuple[str, str]] = [("rows", str(row_count))]
        if "oldest" in ts:
            table_rows += [
                ("oldest", str(ts["oldest"]) if ts["oldest"] is not None else "—"),
                ("newest", str(ts["newest"]) if ts["newest"] is not None else "—"),
            ]
        if "uncompressed_bytes" in ts:
            table_rows += [
                ("uncompressed", _fmt_bytes(int(ts["uncompressed_bytes"]))),  # type: ignore[arg-type]
                ("compressed", _fmt_bytes(int(ts["compressed_bytes"]))),  # type: ignore[arg-type]
            ]
        for k, v in ts.items():
            if k.startswith("with_"):
                col = k[len("with_"):]
                table_rows.append((f"with {col}", f"{v} / {row_count}"))
        console.print(Panel(_kv_table(table_rows), title=f"{table_name} ({row_count} rows)"))
