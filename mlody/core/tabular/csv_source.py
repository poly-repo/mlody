"""Typed CSV-backed tabular source implementation."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv

from mlody.core.tabular.interfaces import PreviewResult, QueryInput


def _expand_csv_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Expand CSV path patterns to concrete files, preserving unmatched literals."""
    expanded: list[str] = []
    for path in paths:
        normalized = os.path.expanduser(path)
        matches = glob.glob(normalized, recursive=True) if glob.has_magic(normalized) else [normalized]
        if matches:
            expanded.extend(sorted(matches))
        else:
            expanded.append(normalized)
    return tuple(expanded)


def _read_options(*, header_required: bool) -> pacsv.ReadOptions:
    """Return Arrow CSV read options for the requested header behavior."""
    return pacsv.ReadOptions(autogenerate_column_names=not header_required)


@dataclass(frozen=True)
class CsvSource:
    """A queryable CSV source backed by one or more local paths."""

    paths: tuple[str, ...]
    separator: str = ","
    header_required: bool = True
    content_hash: str | None = None

    def __init__(
        self,
        paths: tuple[str, ...] | list[str] | tuple[Path, ...] | list[Path],
        *,
        separator: str = ",",
        header_required: bool = True,
        content_hash: str | None = None,
    ) -> None:
        object.__setattr__(self, "paths", tuple(str(path) for path in paths))
        object.__setattr__(self, "separator", separator)
        object.__setattr__(self, "header_required", header_required)
        object.__setattr__(self, "content_hash", content_hash)

    def _load_table(self) -> pa.Table:
        """Load and concatenate all CSV fragments into one Arrow table."""
        tables: list[pa.Table] = []
        read_options = _read_options(header_required=self.header_required)
        parse_options = pacsv.ParseOptions(delimiter=self.separator)
        for path in _expand_csv_paths(self.paths):
            materialized = Path(path)
            if not materialized.exists():
                continue
            tables.append(
                pacsv.read_csv(
                    materialized,
                    read_options=read_options,
                    parse_options=parse_options,
                )
            )
        if not tables:
            raise FileNotFoundError(
                f"No CSV files found for source paths: {list(self.paths)!r}"
            )
        if len(tables) == 1:
            return tables[0]
        return pa.concat_tables(tables)

    def preview(self, limit: int) -> PreviewResult:
        """Return a limited preview plus the full row count."""
        table = self._load_table()
        limited = table.slice(0, limit)
        return PreviewResult(table=limited, total_rows=table.num_rows)

    def count(self) -> int:
        """Return the total row count across the CSV source."""
        return self._load_table().num_rows

    def materialize(self) -> Path:
        """Return the first concrete CSV file backing the source."""
        for path in _expand_csv_paths(self.paths):
            materialized = Path(path)
            if materialized.exists():
                return materialized
        raise FileNotFoundError(
            f"No CSV files found for source paths: {list(self.paths)!r}"
        )

    def query_input(self) -> QueryInput:
        """Return the Arrow-table input shape expected by ``mlody_query``."""
        return self._load_table()
