"""Generic asset resolution for runtime values and locations."""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path

from mlody.common.struct import Struct
from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.http_asset import HttpAssetSource
from mlody.core.assets.interfaces import AssetSource, MaterializedAsset
from mlody.core.assets.local_asset import LocalPathAssetSource
from mlody.core.lineage import build_lineage_event, record_lineage
from mlody.core.location_specs import (
    PosixLocationSpec,
    RemoteLocationSpec,
    _source_value_struct,
    derived_location_spec_from_value,
)


@dataclass(frozen=True, slots=True)
class _RemoteLineageAssetSource:
    """Wrap a remote asset so materialization records lineage on its owner."""

    upstream: AssetSource
    lineage_owner: object
    remote_spec: RemoteLocationSpec
    location: object

    def materialize(self) -> MaterializedAsset:
        materialized = self.upstream.materialize()
        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data=self.remote_spec.uri),
            source="downloaded from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "remote-download",
                "uri": self.remote_spec.uri,
                "staged_path": str(materialized.path),
                "content_hash": materialized.content_hash,
                "location": _location_lineage_payload(self.location),
            },
        )
        record_lineage(self.lineage_owner, event)
        return materialized


def asset_from_location(
    location: object,
    *,
    freshness: object | None = None,
    db_conn: object | None = None,
    asset_id: str | None = None,
) -> AssetSource | None:
    """Resolve a runtime location object into a generic asset source."""
    remote_spec = RemoteLocationSpec.from_location(location)
    if remote_spec is not None:
        return HttpAssetSource(
            uri=remote_spec.uri,
            freshness=freshness,
            db_conn=db_conn,  # type: ignore[arg-type]
            asset_id=asset_id,
        )

    posix_spec = PosixLocationSpec.from_location(location)
    if posix_spec is not None:
        return _local_asset_from_spec(posix_spec)

    return None


def asset_from_value(
    value_struct: object,
    *,
    freshness_override: object | None = None,
    db_conn: object | None = None,
) -> AssetSource | None:
    """Resolve a runtime value struct into a generic asset source."""
    if derived_location_spec_from_value(value_struct) is not None:
        return None

    location = getattr(value_struct, "location", None)
    posix_spec = PosixLocationSpec.from_location(location)
    source_value = _source_value_struct(value_struct)
    freshness = (
        freshness_override
        if freshness_override is not None
        else getattr(value_struct, "freshness", None)
    )
    if posix_spec is not None and source_value is not None:
        return _copied_asset_from_value(
            value_struct,
            posix_spec,
            source_value,
            freshness=freshness,
        )

    asset_id: str | None = None
    remote_spec = RemoteLocationSpec.from_location(location)
    if remote_spec is not None and db_conn is not None:
        from mlody.db.assets import upsert_external_asset  # noqa: PLC0415
        from mlody.core.assets.manifest import cache_key_for_uri  # noqa: PLC0415
        from mlody.core.assets.freshness_policy import freshness_policy_from_struct  # noqa: PLC0415

        policy = freshness_policy_from_struct(freshness)
        max_age_seconds = (
            int(policy.max_age.total_seconds()) if policy.max_age is not None else None
        )
        rep = getattr(getattr(value_struct, "representation", None), "name", None)
        asset_id = upsert_external_asset(
            db_conn,  # type: ignore[arg-type]
            uri=remote_spec.uri,
            transport="http",
            cache_key=cache_key_for_uri(remote_spec.uri),
            representation=rep,
            freshness_kind=policy.kind,
            freshness_max_age_seconds=max_age_seconds,
            value_name=str(getattr(value_struct, "name", None) or ""),
        )

    asset = asset_from_location(location, freshness=freshness, db_conn=db_conn, asset_id=asset_id)
    if asset is not None and remote_spec is not None:
        return _RemoteLineageAssetSource(
            upstream=asset,
            lineage_owner=value_struct,
            remote_spec=remote_spec,
            location=location,
        )
    return asset


def _local_asset_from_spec(spec: PosixLocationSpec) -> LocalPathAssetSource | None:
    if len(spec.paths) != 1:
        return None
    raw_path = spec.paths[0]
    if glob.has_magic(raw_path):
        return None
    return LocalPathAssetSource(path=Path(raw_path).expanduser())


def _copied_asset_from_value(
    value_struct: object,
    posix_spec: PosixLocationSpec,
    source_value: object,
    *,
    freshness: object | None,
) -> CopiedAssetSource | None:
    if len(posix_spec.paths) != 1:
        return None

    source_attr = getattr(value_struct, "source", None)
    source_label = source_attr if isinstance(source_attr, str) else getattr(
        source_attr,
        "name",
        None,
    )
    value_name = str(getattr(value_struct, "name", "<unknown>"))
    source_name = str(getattr(source_value, "name", source_label or "<unknown>"))

    def _make_upstream(
        source_struct: object = source_value,
        source_name: str = source_name,
        value_name: str = value_name,
        freshness: object | None = freshness,
    ) -> AssetSource:
        upstream = asset_from_value(source_struct, freshness_override=freshness)
        if upstream is None:
            raise ValueError(
                f"Source-backed local value {value_name!r} depends on non-materializable "
                f"source {source_name!r} in v1"
            )
        return upstream

    return CopiedAssetSource(
        value_name=value_name,
        destination_path=posix_spec.paths[0],
        upstream_factory=_make_upstream,
        source_label=str(source_label) if source_label is not None else None,
        lineage_owner=value_struct,
        freshness=freshness,
    )


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
