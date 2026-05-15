"""Generic asset resolution for runtime values and locations."""

from __future__ import annotations

import glob
from pathlib import Path

from mlody.core.assets.http_asset import HttpAssetSource
from mlody.core.assets.interfaces import AssetSource
from mlody.core.assets.local_asset import LocalPathAssetSource
from mlody.core.tabular.location_specs import (
    PosixLocationSpec,
    RemoteLocationSpec,
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
    if _source_value_struct(value_struct) is not None:
        return None
    return asset_from_location(getattr(value_struct, "location", None))


def _source_value_struct(value_struct: object) -> object | None:
    """Return the embedded source value struct when present."""
    source_value = getattr(value_struct, "_source_value", None)
    if source_value is not None:
        return source_value
    source_attr = getattr(value_struct, "source", None)
    if getattr(source_attr, "kind", None) == "value":
        return source_attr
    return None


def _local_asset_from_spec(spec: PosixLocationSpec) -> LocalPathAssetSource | None:
    if len(spec.paths) != 1:
        return None
    raw_path = spec.paths[0]
    if glob.has_magic(raw_path):
        return None
    return LocalPathAssetSource(path=Path(raw_path).expanduser())

