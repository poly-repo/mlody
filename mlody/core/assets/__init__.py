"""Asset-layer entry points for artifact materialization.

New code should usually start with:

- ``asset_from_value(...)`` for runtime values
- ``asset_from_location(...)`` for bare locations

The tabular layer now adapts CSV/parquet behavior on top of these asset
sources, while remote caching, local copy refresh, and transfer lineage live
here.
"""

from mlody.core.assets.cache import cache_dir_for_key, default_http_cache_root, ensure_cache_root
from mlody.core.assets.copied_asset import CopiedAssetSource
from mlody.core.assets.freshness_policy import (
    FreshnessPolicy,
    freshness_policy_from_struct,
    manifest_with_refreshed_remote_metadata,
    remote_metadata_indicates_change,
    should_refresh_copied_asset,
    should_revalidate_http_asset,
)
from mlody.core.assets.http_asset import HttpAssetError, HttpAssetSource
from mlody.core.assets.interfaces import AssetSource, MaterializedAsset
from mlody.core.assets.local_asset import LocalAssetError, LocalPathAssetSource
from mlody.core.assets.manifest import (
    MANIFEST_SCHEMA_VERSION,
    HttpAssetManifest,
    HttpAssetManifestLocal,
    HttpAssetManifestRemote,
    HttpAssetManifestRequest,
    cache_key_for_uri,
    load_manifest,
    write_manifest,
)
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.assets.resolution import asset_from_location, asset_from_value
from mlody.core.assets.ssh_asset import SshAssetError, SshAssetSource

__all__ = [
    "AssetMetadata",
    "AssetSource",
    "CopiedAssetSource",
    "FreshnessPolicy",
    "HttpAssetError",
    "HttpAssetManifest",
    "HttpAssetManifestLocal",
    "HttpAssetManifestRemote",
    "HttpAssetManifestRequest",
    "HttpAssetSource",
    "LocalAssetError",
    "LocalPathAssetSource",
    "MANIFEST_SCHEMA_VERSION",
    "MaterializedAsset",
    "cache_dir_for_key",
    "cache_key_for_uri",
    "default_http_cache_root",
    "ensure_cache_root",
    "asset_from_location",
    "asset_from_value",
    "freshness_policy_from_struct",
    "load_manifest",
    "manifest_with_refreshed_remote_metadata",
    "remote_metadata_indicates_change",
    "should_refresh_copied_asset",
    "should_revalidate_http_asset",
    "SshAssetError",
    "SshAssetSource",
    "write_manifest",
]
