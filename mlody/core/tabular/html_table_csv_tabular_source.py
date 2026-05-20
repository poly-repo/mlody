"""Tabular adapter that converts the first HTML table into a local CSV artifact."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from mlody.core.assets.freshness_policy import should_refresh_copied_asset
from mlody.core.assets.interfaces import AssetSource
from mlody.core.tabular.interfaces import PreviewResult, QueryInput


@dataclass(frozen=True)
class HtmlTableCsvTabularSource:
    """Materialize a local CSV by reading the first table from an upstream HTML asset."""

    value_name: str
    destination_path: str
    upstream_factory: Callable[[], AssetSource] | None = None
    source_label: str | None = None
    separator: str = ","
    header_required: bool = True
    freshness: object | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        """Expose the concrete local CSV path for downstream consumers."""
        return (str(self._destination()),)

    def _destination(self) -> Path:
        return Path(os.path.expanduser(self.destination_path))

    def _local_source(self):
        from mlody.core.tabular.csv_source import CsvSource

        return CsvSource(
            paths=(self.materialize(),),
            separator=self.separator,
            header_required=self.header_required,
        )

    def preview(self, limit: int) -> PreviewResult:
        """Return a preview from the converted CSV, materializing first if needed."""
        return self._local_source().preview(limit)

    def count(self) -> int:
        """Return the row count of the converted CSV."""
        return self._local_source().count()

    def materialize(self) -> Path:
        """Ensure the converted CSV exists and return its path."""
        destination = self._destination()
        if destination.exists() and not should_refresh_copied_asset(
            self.freshness,
            destination_mtime=destination.stat().st_mtime,
        ):
            return destination

        if self.upstream_factory is None:
            source_ref = self.source_label or "<unknown>"
            raise ValueError(
                f"Source-backed local value {self.value_name!r} cannot materialize "
                f"source {source_ref!r} because no resolved upstream source is available"
            )

        upstream_asset = self.upstream_factory().materialize()
        source_path = upstream_asset.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(destination) + ".tmp")
        try:
            tables = pd.read_html(str(source_path), flavor="html5lib")
            if not tables:
                raise ValueError(f"No HTML tables found in source {source_path}")
            tables[0].to_csv(
                tmp_path,
                index=False,
                sep=self.separator,
                header=self.header_required,
            )
            os.replace(tmp_path, destination)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        return destination

    def query_input(self) -> QueryInput:
        """Return the Arrow-table input shape expected by ``mlody_query``."""
        return self._local_source().query_input()
