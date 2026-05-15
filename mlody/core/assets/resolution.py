"""Generic asset resolution for runtime values and locations."""

from __future__ import annotations

import glob
from pathlib import Path

from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.http_asset import HttpAssetSource
from mlody.core.assets.interfaces import AssetSource
from mlody.core.assets.local_asset import LocalPathAssetSource
from mlody.core.location_specs import (
    PosixLocationSpec,
    RemoteLocationSpec,
    _source_value_struct,
    derived_location_spec_from_value,
)


def asset_from_location(location: object) -> AssetSource | None:
    """Resolve a runtime location object into a generic asset source."""
    remote_spec = RemoteLocationSpec.from_location(location)
    if remote_spec is not None:
        return HttpAssetSource(uri=remote_spec.uri)

    posix_spec = PosixLocationSpec.from_location(location)
    if posix_spec is not None:
        return _local_asset_from_spec(posix_spec)

    return None


def asset_from_value(value_struct: object) -> AssetSource | None:
    """Resolve a runtime value struct into a generic asset source."""
    if derived_location_spec_from_value(value_struct) is not None:
        return None

    location = getattr(value_struct, "location", None)
    posix_spec = PosixLocationSpec.from_location(location)
    source_value = _source_value_struct(value_struct)
    if posix_spec is not None and source_value is not None:
        return _copied_asset_from_value(value_struct, posix_spec, source_value)

    return asset_from_location(location)
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
    ) -> AssetSource:
        upstream = asset_from_value(source_struct)
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
    )
