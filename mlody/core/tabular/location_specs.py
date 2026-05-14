"""Typed location adapters and tabular-source factories for the Python runtime."""

from __future__ import annotations

import glob
import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from mlody.common.struct import Struct
from mlody.core.lineage import build_lineage_event, record_lineage
from mlody.core.tabular.interfaces import QuerySpec, TabularSource


def _specific_kind(location: object) -> str:
    """Return the most specific location discriminator available."""
    return (
        getattr(location, "_root_kind", None)
        or getattr(location, "type", None)
        or getattr(location, "kind", "")
    )


def _coerce_path_tuple(path_value: object) -> tuple[str, ...]:
    """Convert a location path payload to a tuple of strings."""
    if path_value is None:
        return ()
    if isinstance(path_value, str):
        return (path_value,)
    if isinstance(path_value, Path):
        return (str(path_value),)
    if isinstance(path_value, (list, tuple)):
        return tuple(str(path) for path in path_value)
    return (str(path_value),)


def _paths_from_location(location: object) -> tuple[str, ...]:
    """Extract path strings from either direct fields or ``attributes``."""
    direct = getattr(location, "path", None)
    if direct is not None:
        return _coerce_path_tuple(direct)
    attrs = getattr(location, "attributes", None)
    if isinstance(attrs, dict):
        return _coerce_path_tuple(attrs.get("path"))
    return ()


def _source_value_struct(value_struct: object) -> object | None:
    """Return the embedded source value struct when available."""
    source_value = getattr(value_struct, "_source_value", None)
    if source_value is not None:
        return source_value
    source_attr = getattr(value_struct, "source", None)
    if getattr(source_attr, "kind", None) == "value":
        return source_attr
    return None


def _representation_name(value_struct: object) -> str | None:
    """Return the representation discriminator for a value struct, if present."""
    representation = getattr(value_struct, "representation", None)
    if representation is None:
        return None
    return getattr(representation, "name", None) or getattr(representation, "type", None)


def _representation_bool(value_struct: object, attr_name: str, default: bool = False) -> bool:
    """Return a bool representation attribute with a fallback default."""
    representation = getattr(value_struct, "representation", None)
    if representation is None:
        return default
    direct = getattr(representation, attr_name, None)
    if isinstance(direct, bool):
        return direct
    attrs = getattr(representation, "attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get(attr_name), bool):
        return bool(attrs[attr_name])
    return default


def _representation_string(value_struct: object, attr_name: str, default: str) -> str:
    """Return a string representation attribute with a fallback default."""
    representation = getattr(value_struct, "representation", None)
    if representation is None:
        return default
    direct = getattr(representation, attr_name, None)
    if isinstance(direct, str):
        return direct
    attrs = getattr(representation, "attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get(attr_name), str):
        return str(attrs[attr_name])
    return default


def _location_lineage_payload(location: object) -> dict[str, object]:
    payload: dict[str, object] = {}

    kind = getattr(location, "kind", None)
    if kind is not None:
        payload["kind"] = str(kind)

    location_type = getattr(location, "type", None)
    if location_type is not None:
        payload["type"] = str(location_type)

    path = getattr(location, "path", None)
    if path is not None:
        payload["path"] = (
            [str(segment) for segment in path]
            if isinstance(path, (list, tuple))
            else str(path)
        )

    attributes = getattr(location, "attributes", None)
    if isinstance(attributes, dict):
        payload["attributes"] = dict(attributes)

    return payload


def _record_remote_download_lineage(
    value_struct: object,
    *,
    remote_spec: "RemoteLocationSpec",
    staged_path: Path,
    content_hash: str | None,
) -> None:
    event = build_lineage_event(
        accessor=".location",
        new_value=Struct(kind="location", data=remote_spec.uri),
        source="downloaded from",
        reason=None,
        timestamp=None,
        mode="inplace",
        details={
            "kind": "remote-download",
            "uri": remote_spec.uri,
            "staged_path": str(staged_path),
            "content_hash": content_hash,
            "location": _location_lineage_payload(getattr(value_struct, "location", None)),
        },
    )
    record_lineage(value_struct, event)


def _expand_pattern(path_pattern: str) -> tuple[str, ...]:
    """Expand a filesystem glob pattern, preserving unmatched literals."""
    expanded_pattern = os.path.expanduser(path_pattern)
    matches = tuple(sorted(glob.glob(expanded_pattern)))
    return matches or (path_pattern,)


def _dedupe_preserving_order(paths: Iterable[str]) -> tuple[str, ...]:
    """Return a stable-order tuple with duplicates removed."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return tuple(unique)


def _derived_cache_root() -> Path:
    """Return the writable cache root for derived parquet outputs."""
    test_tmpdir = os.environ.get("TEST_TMPDIR")
    if test_tmpdir:
        return Path(test_tmpdir) / "mlody" / "derived"
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "mlody" / "derived"
    return Path.home() / ".cache" / "mlody" / "derived"


def _derived_cache_path(source_paths: tuple[str, ...], query: QuerySpec) -> Path:
    """Return the deterministic cache path used for composed derived values."""
    raw_key = ":".join(sorted(source_paths)) + ":" + query.dialect + ":" + query.sql
    digest = hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    return _derived_cache_root() / f"{digest}.parquet"


@dataclass(frozen=True)
class PosixLocationSpec:
    """Typed view of a path-backed location used by the runtime."""

    paths: tuple[str, ...]
    name: str = ""
    kind: str = "posix"

    @classmethod
    def from_location(cls, location: object) -> PosixLocationSpec | None:
        """Parse a runtime location object into a typed posix spec."""
        if location is None or _specific_kind(location) == "derived":
            return None
        paths = _paths_from_location(location)
        if not paths:
            return None
        kind = _specific_kind(location) or "posix"
        return cls(paths=paths, name=str(getattr(location, "name", "")), kind=kind)

    def compose(
        self,
        field_spec: PosixLocationSpec | None,
        field_name: str,
    ) -> PosixLocationSpec:
        """Compose this parent path set with a field path set or field name."""
        field_paths = field_spec.paths if field_spec is not None else (field_name,)
        if not field_paths:
            field_paths = (field_name,)

        composed_patterns = tuple(
            os.path.join(parent_path, field_path)
            for parent_path in (self.paths or ("",))
            for field_path in field_paths
        )
        expanded_paths = _dedupe_preserving_order(
            expanded
            for pattern in composed_patterns
            for expanded in _expand_pattern(pattern)
        )
        return PosixLocationSpec(
            paths=expanded_paths or composed_patterns,
            name=self.name,
            kind="posix",
        )

    def to_struct(self) -> Struct:
        """Serialize the typed spec back into the runtime Struct shape."""
        return Struct(
            kind="location",
            type="posix",
            name=self.name,
            path=list(self.paths),
        )


@dataclass(frozen=True)
class RemoteLocationSpec:
    """Typed view of a transport-only remote location."""

    uri: str
    name: str = "remote"

    @classmethod
    def from_location(cls, location: object) -> RemoteLocationSpec | None:
        """Parse a runtime location object into a typed remote spec."""
        if location is None or _specific_kind(location) != "remote":
            return None
        uri = getattr(location, "uri", None)
        if uri is None:
            attrs = getattr(location, "attributes", None)
            if isinstance(attrs, dict):
                uri = attrs.get("uri")
        if not isinstance(uri, str) or uri == "":
            return None
        return cls(uri=uri, name=str(getattr(location, "name", "remote") or "remote"))


@dataclass(frozen=True)
class DerivedLocationSpec:
    """Typed view of a derived location backed by a SQL query over parquet."""

    source_ref: str
    source_paths: tuple[str, ...]
    query: QuerySpec
    output_path: Path
    name: str = "derived"

    @classmethod
    def from_location(cls, location: object) -> DerivedLocationSpec | None:
        """Parse a derived runtime location object into a typed spec."""
        if location is None or _specific_kind(location) != "derived":
            return None
        attrs = getattr(location, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            attrs = {}
        query = QuerySpec(
            sql=str(attrs.get("sql_fragment") or ""),
            dialect=str(attrs.get("dialect") or "duckdb"),
        )
        output_value = attrs.get("output_path")
        source_paths = _coerce_path_tuple(attrs.get("source_paths"))
        output_path = Path(str(output_value)) if output_value else _derived_cache_path(
            source_paths,
            query,
        )
        return cls(
            source_ref=str(attrs.get("source_ref") or ""),
            source_paths=source_paths,
            query=query,
            output_path=output_path,
            name=str(getattr(location, "name", "derived") or "derived"),
        )

    def with_source_paths(self, source_paths: tuple[str, ...]) -> DerivedLocationSpec:
        """Return the same derived spec with explicit resolved source paths."""
        return replace(self, source_paths=source_paths)

    def compose(
        self,
        field_spec: PosixLocationSpec | None,
        field_name: str,
    ) -> DerivedLocationSpec:
        """Compose derived source paths with a nested field path selection."""
        field_paths = field_spec.paths if field_spec is not None else (field_name,)
        if not field_paths:
            field_paths = (field_name,)

        composed_patterns = tuple(
            os.path.join(os.path.expanduser(parent_path), field_path)
            for parent_path in (self.source_paths or ("",))
            for field_path in field_paths
        )
        expanded_paths = _dedupe_preserving_order(
            expanded
            for pattern in composed_patterns
            for expanded in _expand_pattern(pattern)
        )
        new_paths = expanded_paths or composed_patterns
        return replace(
            self,
            source_paths=new_paths,
            output_path=_derived_cache_path(new_paths, self.query),
        )

    def to_struct(self) -> Struct:
        """Serialize the typed derived spec back into the runtime Struct shape."""
        return Struct(
            kind="location",
            type="derived",
            name=self.name,
            abstract=False,
            _root_kind="derived",
            attributes={
                "source_ref": self.source_ref,
                "source_paths": list(self.source_paths),
                "sql_fragment": self.query.sql,
                "dialect": self.query.dialect,
                "output_path": str(self.output_path),
            },
        )


def derived_location_spec_from_value(value_struct: object) -> DerivedLocationSpec | None:
    """Resolve a derived spec from a value struct, filling source-path fallbacks."""
    location = getattr(value_struct, "location", None)
    spec = DerivedLocationSpec.from_location(location)
    if spec is None:
        return None
    if spec.source_paths:
        return spec

    source_struct = getattr(value_struct, "_source_value", None)
    if source_struct is None:
        source_struct = getattr(value_struct, "source", None)
    source_location = getattr(source_struct, "location", None) if source_struct else None
    source_spec = PosixLocationSpec.from_location(source_location)
    if source_spec is not None:
        return spec.with_source_paths(source_spec.paths)
    if isinstance(source_struct, str) and spec.source_ref:
        return spec.with_source_paths((spec.source_ref,))
    return spec


def _remote_derived_output_path(content_hash: str, query: QuerySpec) -> Path:
    """Return a derived cache path keyed by remote content hash plus query."""
    raw_key = content_hash + ":" + query.dialect + ":" + query.sql
    digest = hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    return _derived_cache_root() / f"{digest}.parquet"


def _csv_source_from_paths(
    paths: tuple[str, ...],
    *,
    value_struct: object,
    content_hash: str | None = None,
) -> TabularSource:
    """Construct a CSV source using representation metadata from a value."""
    from mlody.core.tabular.csv_source import CsvSource

    return CsvSource(
        paths=paths,
        separator=_representation_string(value_struct, "separator", ","),
        header_required=_representation_bool(value_struct, "header_required", True),
        content_hash=content_hash,
    )


def _source_backed_local_source_from_value(
    value_struct: object,
    posix_spec: PosixLocationSpec,
) -> TabularSource:
    """Construct a lazy local source backed by another tabular source."""
    from mlody.core.tabular.materialized_local_source import MaterializedLocalSource

    value_name = str(getattr(value_struct, "name", "<unknown>"))
    if len(posix_spec.paths) != 1:
        raise ValueError(
            f"Source-backed local value {value_name!r} requires exactly one "
            "destination path in v1"
        )

    representation_name = _representation_name(value_struct)
    if representation_name not in {"csv", "parquet"}:
        raise ValueError(
            f"Source-backed local value {value_name!r} requires representation=csv() "
            "or representation=parquet() in v1"
        )

    source_attr = getattr(value_struct, "source", None)
    source_value = _source_value_struct(value_struct)
    source_label = source_attr if isinstance(source_attr, str) else getattr(
        source_attr,
        "name",
        None,
    )

    upstream_factory = None
    if source_value is not None:
        source_name = str(getattr(source_value, "name", source_label or "<unknown>"))

        def _make_upstream(
            source_struct: object = source_value,
            source_name: str = source_name,
            value_name: str = value_name,
        ) -> TabularSource:
            upstream = source_from_value(source_struct)
            if upstream is None:
                raise ValueError(
                    f"Source-backed local value {value_name!r} depends on non-tabular "
                    f"source {source_name!r} in v1"
                )
            return upstream

        upstream_factory = _make_upstream

    return MaterializedLocalSource(
        value_name=value_name,
        destination_path=posix_spec.paths[0],
        representation_name=representation_name,
        upstream_factory=upstream_factory,
        source_label=str(source_label) if source_label is not None else None,
        separator=_representation_string(value_struct, "separator", ","),
        header_required=_representation_bool(value_struct, "header_required", True),
        lineage_owner=value_struct,
    )


def _remote_tabular_source(
    value_struct: object,
    remote_spec: RemoteLocationSpec,
) -> TabularSource | None:
    """Construct a staged tabular source for a remote-backed value."""
    from mlody.core.tabular.parquet_source import ParquetSource
    from mlody.core.tabular.remote_staging import stage_remote_file

    if _representation_bool(value_struct, "multifile", False):
        return None

    representation_name = _representation_name(value_struct)
    if representation_name not in {"csv", "parquet"}:
        return None

    staged = stage_remote_file(remote_spec.uri)
    _record_remote_download_lineage(
        value_struct,
        remote_spec=remote_spec,
        staged_path=staged.path,
        content_hash=staged.content_hash,
    )
    staged_path = (str(staged.path),)
    if representation_name == "csv":
        return _csv_source_from_paths(
            staged_path,
            value_struct=value_struct,
            content_hash=staged.content_hash,
        )
    return ParquetSource(paths=staged_path, content_hash=staged.content_hash)


def _derived_source_from_value(
    value_struct: object,
    derived_spec: DerivedLocationSpec,
) -> TabularSource:
    """Construct a derived source with the best available upstream query input."""
    from mlody.core.tabular.derived_source import DerivedSource

    source_value = _source_value_struct(value_struct)
    if source_value is None:
        source_value = getattr(value_struct, "source", None)
    if source_value is None:
        return DerivedSource(spec=derived_spec)

    source_tabular = source_from_value(source_value)
    if source_tabular is None:
        if derived_spec.source_paths:
            return DerivedSource(spec=derived_spec)
        source_name = getattr(source_value, "name", derived_spec.source_ref or "<unknown>")
        raise ValueError(f"Derived source {source_name!r} is not tabular in v1")

    effective_spec = derived_spec
    source_digest = getattr(source_tabular, "content_hash", None)
    if isinstance(source_digest, str) and source_digest:
        effective_spec = replace(
            effective_spec,
            output_path=_remote_derived_output_path(source_digest, effective_spec.query),
        )
    elif not effective_spec.source_paths and hasattr(source_tabular, "paths"):
        source_paths = tuple(str(path) for path in getattr(source_tabular, "paths"))
        effective_spec = effective_spec.with_source_paths(source_paths)

    return DerivedSource(
        spec=effective_spec,
        source_input=source_tabular.query_input(),
    )


def source_from_location(location: object) -> TabularSource | None:
    """Construct a tabular source directly from a runtime location object."""
    from mlody.core.tabular.derived_source import DerivedSource
    from mlody.core.tabular.parquet_source import ParquetSource

    derived_spec = DerivedLocationSpec.from_location(location)
    if derived_spec is not None and derived_spec.source_paths:
        return DerivedSource(spec=derived_spec)

    posix_spec = PosixLocationSpec.from_location(location)
    if posix_spec is not None:
        return ParquetSource(paths=posix_spec.paths)

    return None


def source_from_value(value_struct: object) -> TabularSource | None:
    """Construct the best tabular source view for a runtime value struct."""
    from mlody.core.tabular.parquet_source import ParquetSource

    derived_spec = derived_location_spec_from_value(value_struct)
    if derived_spec is not None:
        return _derived_source_from_value(value_struct, derived_spec)

    location = getattr(value_struct, "location", None)
    remote_spec = RemoteLocationSpec.from_location(location)
    if remote_spec is not None:
        return _remote_tabular_source(value_struct, remote_spec)

    posix_spec = PosixLocationSpec.from_location(location)
    if posix_spec is not None:
        if getattr(value_struct, "source", None) is not None:
            return _source_backed_local_source_from_value(value_struct, posix_spec)
        representation_name = _representation_name(value_struct)
        if representation_name == "csv":
            return _csv_source_from_paths(posix_spec.paths, value_struct=value_struct)
        return ParquetSource(paths=posix_spec.paths)

    return None


def query_rows_from_value(value_struct: object, sql: str) -> list[dict[str, object]]:
    """Run *sql* against a tabular value struct and return row dicts."""
    from mlody.core.sql.sql_query import mlody_query

    value_name = str(getattr(value_struct, "name", "<unknown>"))
    try:
        tabular_source = source_from_value(value_struct)
    except ValueError as exc:
        raise ValueError(
            f"Failed to prepare tabular value {value_name!r} for SQL query: {exc}"
        ) from exc

    if tabular_source is None:
        raise ValueError(
            f"SQL entity queries require a tabular value; {value_name!r} is not tabular in v1"
        )

    try:
        table = mlody_query(tabular_source.query_input(), sql)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"SQL query failed for value {value_name!r}: {exc}"
        ) from exc

    return table.to_pylist()
