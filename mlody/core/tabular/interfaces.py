"""Small typed interfaces for tabular sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa


@dataclass(frozen=True)
class QuerySpec:
    """A query fragment plus the dialect used to interpret it."""

    sql: str
    dialect: str = "duckdb"


@dataclass(frozen=True)
class PreviewResult:
    """A limited preview table paired with the full source row count."""

    table: pa.Table
    total_rows: int


class TabularSource(Protocol):
    """Protocol shared by concrete queryable tabular sources."""

    def preview(self, limit: int) -> PreviewResult: ...

    def count(self) -> int: ...

    def materialize(self) -> Path: ...
