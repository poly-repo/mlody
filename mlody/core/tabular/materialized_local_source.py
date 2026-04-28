"""Lazy materialization of source-backed local tabular artifacts."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mlody.core.tabular.interfaces import PreviewResult, QueryInput, TabularSource

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterializedLocalSource:
    """A local tabular artifact backed by an upstream tabular source."""

    value_name: str
    destination_path: str
    representation_name: str
    upstream_factory: Callable[[], TabularSource] | None = None
    source_label: str | None = None
    separator: str = ","
    header_required: bool = True

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
        destination = self._destination()
        if destination.exists():
            _logger.debug("Source-backed local cache hit for %s", destination)
            return destination

        if self.upstream_factory is None:
            source_ref = self.source_label or "<unknown>"
            raise ValueError(
                f"Source-backed local value {self.value_name!r} cannot materialize "
                f"source {source_ref!r} because no resolved upstream source is available"
            )

        upstream = self.upstream_factory()
        source_path = upstream.materialize()

        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(destination) + ".tmp")
        _logger.info(
            "Copying source-backed local artifact for %s from %s to %s",
            self.value_name,
            source_path,
            destination,
        )
        try:
            shutil.copyfile(source_path, tmp_path)
            os.replace(tmp_path, destination)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        _logger.info(
            "Materialized source-backed local artifact for %s at %s (%d bytes)",
            self.value_name,
            destination,
            destination.stat().st_size,
        )
        return destination

    def query_input(self) -> QueryInput:
        """Return the query input for the local artifact after materialization."""
        return self._local_source().query_input()
