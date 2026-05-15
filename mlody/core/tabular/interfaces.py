"""Small typed interfaces for tabular sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from mlody.core.query_spec import QuerySpec


@dataclass(frozen=True)
class PreviewResult:
    """A limited preview table paired with the full source row count."""

    table: pa.Table
    total_rows: int


QueryInput = str | Path | list[str | Path] | pa.Table


class TabularSource(Protocol):
    """Protocol shared by concrete queryable tabular sources."""

    def preview(self, limit: int) -> PreviewResult: ...

    def count(self) -> int: ...

    def materialize(self) -> Path: ...

    def query_input(self) -> QueryInput: ...
