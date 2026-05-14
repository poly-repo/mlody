"""Lazy materialization of source-backed local tabular artifacts."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mlody.common.struct import Struct
from mlody.core.lineage import build_lineage_event, record_lineage
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
        destination = self._destination()
        if destination.exists():
            _logger.debug("Source-backed local cache hit for %s", destination)
            self._record_copy_lineage(destination)
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
        self._record_copy_lineage(destination, source_path=source_path)
        return destination

    def query_input(self) -> QueryInput:
        """Return the query input for the local artifact after materialization."""
        return self._local_source().query_input()

    def _record_copy_lineage(
        self,
        destination: Path,
        *,
        source_path: Path | None = None,
    ) -> None:
        if self.lineage_owner is None:
            return

        for source_event in self._inherited_source_lineage_events(source_path=source_path):
            record_lineage(self.lineage_owner, source_event)

        existing_lineage = getattr(self.lineage_owner, "_lineage", None)
        if isinstance(existing_lineage, list):
            for existing_event in existing_lineage:
                if getattr(existing_event, "source", None) != "copied from":
                    continue
                existing_details = getattr(existing_event, "details", None)
                if not isinstance(existing_details, dict):
                    continue
                if (
                    existing_details.get("destination_path") == str(destination)
                    and existing_details.get("source_label") == self.source_label
                ):
                    return

        source_ref = self.source_label or (
            str(source_path) if source_path is not None else "<unknown>"
        )
        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data=source_ref),
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "local-copy",
                "source_label": self.source_label,
                "source_path": str(source_path) if source_path is not None else None,
                "destination_path": str(destination),
            },
        )
        record_lineage(self.lineage_owner, event)

    def _inherited_source_lineage_events(
        self,
        *,
        source_path: Path | None,
    ) -> list[object]:
        if self.lineage_owner is None:
            return []
        source_value = getattr(self.lineage_owner, "_source_value", None)
        if source_value is None:
            return []
        return self._lineage_events_for_value(
            source_value,
            source_path=source_path,
        )

    def _lineage_events_for_value(
        self,
        value: object,
        *,
        source_path: Path | None,
    ) -> list[object]:
        events: list[object] = []

        nested_source = getattr(value, "_source_value", None)
        if nested_source is not None:
            for nested_event in self._lineage_events_for_value(
                nested_source,
                source_path=None,
            ):
                if nested_event not in events:
                    events.append(nested_event)

        existing_lineage = getattr(value, "_lineage", None)
        if isinstance(existing_lineage, list) and existing_lineage:
            for existing_event in existing_lineage:
                if existing_event not in events:
                    events.append(existing_event)
            return events

        synthesized_event = self._synthesized_lineage_event(
            value,
            source_path=source_path,
        )
        if synthesized_event is not None and synthesized_event not in events:
            events.append(synthesized_event)
        return events

    def _synthesized_lineage_event(
        self,
        value: object,
        *,
        source_path: Path | None,
    ) -> object | None:
        location = getattr(value, "location", None)
        if location is None:
            return None

        location_type = getattr(location, "type", None)
        if location_type == "remote":
            uri = self._location_uri(location)
            if uri is None:
                return None
            details: dict[str, object] = {
                "kind": "remote-download",
                "uri": uri,
                "location": self._location_payload(location),
            }
            if source_path is not None:
                details["staged_path"] = str(source_path)
            return build_lineage_event(
                accessor=".location",
                new_value=Struct(kind="location", data=uri),
                source="downloaded from",
                reason=None,
                timestamp=None,
                mode="inplace",
                details=details,
            )

        source_label = getattr(value, "source", None)
        nested_source = getattr(value, "_source_value", None)
        if source_label is None or nested_source is None:
            return None

        declared_destination = self._location_destination_path(location)
        source_ref = source_label if isinstance(source_label, str) else getattr(
            source_label,
            "name",
            None,
        )
        if source_ref is not None:
            lineage_data = str(source_ref)
        elif source_path is not None:
            lineage_data = str(source_path)
        else:
            lineage_data = "<unknown>"
        return build_lineage_event(
            accessor=".location",
            new_value=Struct(
                kind="location",
                data=lineage_data,
            ),
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "local-copy",
                "source_label": str(source_ref) if source_ref is not None else None,
                "source_path": str(source_path) if source_path is not None else None,
                "destination_path": declared_destination,
            },
        )

    def _location_uri(self, location: object) -> str | None:
        attributes = getattr(location, "attributes", None)
        if isinstance(attributes, dict) and attributes.get("uri") is not None:
            return str(attributes["uri"])
        uri = getattr(location, "uri", None)
        if uri is None:
            return None
        return str(uri)

    def _location_payload(self, location: object) -> dict[str, object]:
        payload: dict[str, object] = {}
        kind = getattr(location, "kind", None)
        if kind is not None:
            payload["kind"] = str(kind)
        location_type = getattr(location, "type", None)
        if location_type is not None:
            payload["type"] = str(location_type)
        path_value = getattr(location, "path", None)
        if path_value is not None:
            payload["path"] = self._path_text(path_value)
        attributes = getattr(location, "attributes", None)
        if isinstance(attributes, dict):
            payload["attributes"] = dict(attributes)
        return payload

    def _location_destination_path(self, location: object) -> str | None:
        path_value = getattr(location, "path", None)
        if path_value is None:
            attributes = getattr(location, "attributes", None)
            if isinstance(attributes, dict):
                path_value = attributes.get("path")
        if path_value is None:
            return None
        if isinstance(path_value, (list, tuple)):
            if not path_value:
                return None
            return str(Path(os.path.expanduser(str(path_value[0]))))
        return str(Path(os.path.expanduser(str(path_value))))

    def _path_text(self, value: object) -> str | list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return str(value)
