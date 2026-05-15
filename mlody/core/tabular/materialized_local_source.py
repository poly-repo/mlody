"""Lazy tabular adapter for source-backed local copied assets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.interfaces import AssetSource, MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.tabular.interfaces import PreviewResult, QueryInput, TabularSource


@dataclass(frozen=True)
class MaterializedLocalSource:
    """A local tabular artifact backed by another source via copied asset logic."""

    value_name: str
    destination_path: str
    representation_name: str
    upstream_factory: Callable[[], TabularSource] | None = None
    source_label: str | None = None
    separator: str = ","
    header_required: bool = True
    lineage_owner: object | None = None

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
        upstream_factory = None
        if self.upstream_factory is not None:

            def _make_upstream() -> AssetSource:
                upstream = self.upstream_factory()
                return _TabularAssetSource(upstream)

            upstream_factory = _make_upstream

        return CopiedAssetSource(
            value_name=self.value_name,
            destination_path=self.destination_path,
            upstream_factory=upstream_factory,
            source_label=self.source_label,
            lineage_owner=self.lineage_owner,
        )


@dataclass(frozen=True)
class _TabularAssetSource:
    """Bridge a tabular source into the generic asset-source protocol."""

    tabular_source: TabularSource

    def materialize(self) -> MaterializedAsset:
        path = self.tabular_source.materialize()
        return MaterializedAsset(
            path=path,
            content_hash=None,
            metadata=AssetMetadata(
                uri=None,
                resolved_url=None,
                digest=None,
                digest_type=None,
                length=None,
                update_time=None,
                cache_key=None,
                transport="posix",
                extra={"path": str(path)},
            ),
        )
