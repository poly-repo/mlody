"""Typed parquet-backed tabular source implementation."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from mlody.core.sql.sql_query import mlody_query
from mlody.core.tabular.interfaces import PreviewResult, QueryInput


def _normalize_query_path(path: str | Path) -> str:
    """Normalize a path for querying, expanding ``~`` and directories."""
    expanded = os.path.expanduser(str(path))
    if os.path.isdir(expanded):
        return os.path.join(expanded, "**", "*.parquet")
    return expanded


@dataclass(frozen=True)
class ParquetSource:
    """A queryable parquet source backed by one or more path patterns."""

    paths: tuple[str, ...]
    content_hash: str | None = None

    def __init__(
        self,
        paths: tuple[str, ...] | list[str] | tuple[Path, ...] | list[Path],
        *,
        content_hash: str | None = None,
    ) -> None:
        normalized = tuple(_normalize_query_path(path) for path in paths)
        object.__setattr__(self, "paths", normalized)
        object.__setattr__(self, "content_hash", content_hash)

    def query_paths(self) -> str | list[str]:
        """Return the path payload shape expected by ``mlody_query``."""
        if len(self.paths) == 1:
            return self.paths[0]
        return list(self.paths)

    def schema_names(self) -> tuple[str, ...]:
        """Return schema names from the first readable parquet file, if any."""
        for path in self.paths:
            matches = glob.glob(path, recursive=True) if glob.has_magic(path) else [path]
            for match in matches:
                try:
                    return tuple(pq.read_schema(match).names)
                except Exception:
                    continue
        return ()

    def preview(self, limit: int) -> PreviewResult:
        """Return a limited preview plus the full row count."""
        table = mlody_query(self.query_paths(), f"SELECT * LIMIT {limit}")
        return PreviewResult(table=table, total_rows=self.count())

    def count(self) -> int:
        """Return the total row count across the parquet source."""
        count_table = mlody_query(self.query_paths(), "SELECT COUNT(*) as n")
        return int(count_table.column("n")[0].as_py())

    def materialize(self) -> Path:
        """Return the first concrete parquet file backing the source."""
        for path in self.paths:
            matches = glob.glob(path, recursive=True) if glob.has_magic(path) else [path]
            for match in matches:
                materialized = Path(match)
                if materialized.exists():
                    return materialized
        raise FileNotFoundError(
            f"No parquet files found for source paths: {list(self.paths)!r}"
        )

    def query_input(self) -> QueryInput:
        """Return the query input shape expected by ``mlody_query``."""
        return self.query_paths()
