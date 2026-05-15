"""Typed location adapters and tabular-source factories for the Python runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from mlody.core.assets.freshness_policy import freshness_policy_from_struct
from mlody.core.assets.http_asset import HttpAssetSource
from mlody.common.struct import Struct
from mlody.core.assets.interfaces import AssetSource, MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.assets.resolution import asset_from_value
from mlody.core.lineage import build_lineage_event, record_lineage
from mlody.core.location_specs import (
    DerivedLocationSpec,
    PosixLocationSpec,
    RemoteLocationSpec,
    _derived_cache_root,
    _source_value_struct,
    derived_location_spec_from_value,
)
from mlody.core.tabular.interfaces import QuerySpec, TabularSource


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


@dataclass(frozen=True)
class _StagedRemoteAssetSource:
    """Asset adapter that preserves the remote staging seam for copied locals."""

    remote_spec: RemoteLocationSpec
    lineage_owner: object | None = None
    freshness: object | None = None

    def materialize(self) -> MaterializedAsset:
        policy = freshness_policy_from_struct(self.freshness)
        if policy.kind in {"always", "ttl"}:
            materialized = HttpAssetSource(
                uri=self.remote_spec.uri,
                freshness=self.freshness,
            ).materialize()
            if self.lineage_owner is not None:
                _record_remote_download_lineage(
                    self.lineage_owner,
                    remote_spec=self.remote_spec,
                    staged_path=materialized.path,
                    content_hash=materialized.content_hash,
                )
            return materialized

        from mlody.core.tabular.remote_staging import stage_remote_file

        staged = stage_remote_file(self.remote_spec.uri)
        if self.lineage_owner is not None:
            _record_remote_download_lineage(
                self.lineage_owner,
                remote_spec=self.remote_spec,
                staged_path=staged.path,
                content_hash=staged.content_hash,
            )
        return MaterializedAsset(
            path=staged.path,
            content_hash=staged.content_hash,
            metadata=AssetMetadata(
                uri=self.remote_spec.uri,
                resolved_url=self.remote_spec.uri,
                digest=None,
                digest_type=None,
                length=None,
                update_time=None,
                cache_key=None,
                transport="http",
                extra={"staged_path": str(staged.path)},
            ),
        )


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


def _tabular_source_from_asset(
    value_struct: object,
    asset: AssetSource,
    *,
    default_to_parquet: bool = False,
) -> TabularSource | None:
    """Wrap a generic asset in the matching tabular adapter."""
    from mlody.core.assets.local_asset import LocalPathAssetSource
    from mlody.core.tabular.parquet_source import ParquetSource

    if _representation_bool(value_struct, "multifile", False):
        return None

    representation_name = _representation_name(value_struct)
    if representation_name not in {"csv", "parquet"}:
        if not (default_to_parquet and representation_name is None):
            return None
        representation_name = "parquet"

    content_hash: str | None = None
    if isinstance(asset, LocalPathAssetSource):
        materialized_paths = (str(asset.path),)
    else:
        materialized = asset.materialize()
        location = getattr(value_struct, "location", None)
        remote_spec = RemoteLocationSpec.from_location(location)
        if remote_spec is not None:
            _record_remote_download_lineage(
                value_struct,
                remote_spec=remote_spec,
                staged_path=materialized.path,
                content_hash=materialized.content_hash,
            )
        materialized_paths = (str(materialized.path),)
        content_hash = materialized.content_hash

    if representation_name == "csv":
        return _csv_source_from_paths(
            materialized_paths,
            value_struct=value_struct,
            content_hash=content_hash,
        )
    return ParquetSource(
        paths=materialized_paths,
        content_hash=content_hash,
    )


def _source_backed_local_source_from_value(
    value_struct: object,
    posix_spec: PosixLocationSpec,
) -> TabularSource:
    """Construct a lazy local source backed by another asset source."""
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
    freshness = getattr(value_struct, "freshness", None)
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
        ) -> AssetSource:
            source_location = getattr(source_struct, "location", None)
            remote_spec = RemoteLocationSpec.from_location(source_location)
            if remote_spec is not None:
                return _StagedRemoteAssetSource(
                    remote_spec=remote_spec,
                    lineage_owner=source_struct,
                    freshness=freshness,
                )
            upstream = asset_from_value(source_struct, freshness_override=freshness)
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
        freshness=freshness,
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
    derived_spec = derived_location_spec_from_value(value_struct)
    if derived_spec is not None:
        return _derived_source_from_value(value_struct, derived_spec)

    location = getattr(value_struct, "location", None)
    posix_spec = PosixLocationSpec.from_location(location)
    if posix_spec is not None:
        if getattr(value_struct, "source", None) is not None:
            return _source_backed_local_source_from_value(value_struct, posix_spec)

    asset = asset_from_value(value_struct)
    if asset is not None:
        asset_source = _tabular_source_from_asset(value_struct, asset)
        if asset_source is not None:
            return asset_source

    if posix_spec is not None:
        representation_name = _representation_name(value_struct)
        if representation_name == "csv":
            return _csv_source_from_paths(posix_spec.paths, value_struct=value_struct)
        from mlody.core.tabular.parquet_source import ParquetSource

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
