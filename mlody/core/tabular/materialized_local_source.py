"""Lazy tabular adapter for source-backed local copied assets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.interfaces import AssetSource
from mlody.core.tabular.interfaces import PreviewResult, QueryInput, TabularSource


@dataclass(frozen=True)
class MaterializedLocalSource:
    """A local tabular artifact backed by another source via copied asset logic."""

    value_name: str
    destination_path: str
    representation_name: str
    upstream_factory: Callable[[], AssetSource] | None = None
    source_label: str | None = None
    separator: str = ","
    header_required: bool = True
    lineage_owner: object | None = None
    freshness: object | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        """Expose the concrete local artifact path for downstream consumers."""
        return (str(self._destination()),)

    def _destination(self) -> Path:
        return Path(os.path.expanduser(self.destination_path))

    def _local_source(self) -> TabularSource:
        from mlody.core.tabular.csv_source import CsvSource
        from mlody.core.tabular.parquet_source import ParquetSource

        materialized_path = self.materialize()
        if self.representation_name == "csv":
            return CsvSource(
                paths=(materialized_path,),
                separator=self.separator,
                header_required=self.header_required,
            )
        return ParquetSource(paths=(materialized_path,))

    def preview(self, limit: int) -> PreviewResult:
        """Return a preview from the local artifact, materializing first if needed."""
        return self._local_source().preview(limit)

    def count(self) -> int:
        """Return the total row count from the local artifact."""
        return self._local_source().count()

    def materialize(self) -> Path:
        """Ensure the declared local artifact exists and return its path."""
        return self._copied_asset().materialize().path

    def query_input(self) -> QueryInput:
        """Return the query input for the local artifact after materialization."""
        return self._local_source().query_input()

    def _copied_asset(self) -> CopiedAssetSource:
        return CopiedAssetSource(
            value_name=self.value_name,
            destination_path=self.destination_path,
            upstream_factory=self.upstream_factory,
            source_label=self.source_label,
            lineage_owner=self.lineage_owner,
            freshness=self.freshness,
        )
