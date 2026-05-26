"""Arrow-backed JSON tabular source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from mlody.core.tabular.interfaces import PreviewResult, QueryInput


@dataclass(frozen=True, slots=True)
class JsonSource:
    """Read a JSON file (array of objects) into an Arrow table."""

    paths: tuple[str, ...]
    content_hash: str | None = None

    def materialize(self) -> Path:
        return Path(self.paths[0])

    def count(self) -> int:
        return int(self._read().num_rows)

    def preview(self, limit: int) -> PreviewResult:
        table = self._read()
        return PreviewResult(table=table.slice(0, limit), total_rows=table.num_rows)

    def query_input(self) -> QueryInput:
        return self._read()

    def _read(self) -> pa.Table:
        import pyarrow.json as pa_json  # noqa: PLC0415

        return pa_json.read_json(self.paths[0])
