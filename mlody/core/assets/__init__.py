"""Asset helpers for persistent, typed artifact materialization."""

from mlody.core.assets.cache import cache_dir_for_key, default_http_cache_root, ensure_cache_root
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

__all__ = [
    "AssetMetadata",
    "AssetSource",
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
    "load_manifest",
    "write_manifest",
]
