"""Persistent HTTP-backed asset source."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.python.http_info import fetch_http_info
from mlody.core.assets.cache import (
    cache_dir_for_key,
    default_http_cache_root,
    ensure_cache_root,
)
from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.manifest import (
    HttpAssetManifest,
    HttpAssetManifestLocal,
    HttpAssetManifestRemote,
    HttpAssetManifestRequest,
    cache_key_for_uri,
    load_manifest,
    write_manifest,
)
from mlody.core.assets.metadata import AssetMetadata

_logger = logging.getLogger(__name__)
_REMOTE_USER_AGENT = "mlody/remote"


class HttpAssetError(ValueError):
    """Raised when an HTTP-backed asset cannot be materialized."""


@dataclass(frozen=True, slots=True)
class HttpAssetSource:
    """Materialize an HTTP/HTTPS asset into the persistent asset cache."""

    uri: str
    cache_root: Path | None = None

    def materialize(self) -> MaterializedAsset:
        """Return a locally cached copy of *uri*."""
        parsed = urlparse(self.uri)
        if parsed.scheme not in {"http", "https"}:
            raise HttpAssetError(
                f"remote(uri=...) only supports http/https in v1, got {parsed.scheme!r}"
            )

        cache_root = self.cache_root or default_http_cache_root()
        ensure_cache_root(cache_root)
        cache_key = cache_key_for_uri(self.uri)
        cache_dir = cache_dir_for_key(cache_root, cache_key)
        manifest_path = cache_dir / "manifest.json"

        cached_asset = self._load_cached_asset(manifest_path)
        if cached_asset is not None:
            _logger.info("Reusing cached remote URI %s from %s", self.uri, cached_asset.path)
            return cached_asset

        return self._download_asset(cache_dir, manifest_path, cache_key)

    def _load_cached_asset(self, manifest_path: Path) -> MaterializedAsset | None:
        if not manifest_path.exists():
            return None

        try:
            manifest = load_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Ignoring unreadable remote asset manifest at %s: %s", manifest_path, exc)
            return None

        payload_relpath = manifest.local.payload_relpath
        content_hash = manifest.local.content_hash
        if not payload_relpath or not content_hash:
            return None

        payload_path = manifest_path.parent / payload_relpath
        if not payload_path.exists():
            return None

        return MaterializedAsset(
            path=payload_path,
            content_hash=content_hash,
            metadata=_asset_metadata_from_manifest(manifest),
        )

    def _download_asset(
        self,
        cache_dir: Path,
        manifest_path: Path,
        cache_key: str,
    ) -> MaterializedAsset:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = fetch_http_info(self.uri)
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Unable to collect remote metadata for %s before download; proceeding with GET only",
                self.uri,
                exc_info=True,
            )
            metadata = {}
        request = Request(self.uri, headers={"User-Agent": _REMOTE_USER_AGENT})
        temp_path: Path | None = None
        resolved_url = self.uri
        try:
            with urlopen(request) as response:  # noqa: S310
                resolved_url = response.geturl()
                suffix = _suffix_for_url(resolved_url or self.uri)
                payload_name = f"payload{suffix}"
                final_path = cache_dir / payload_name
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=cache_dir,
                    prefix=f"{payload_name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    content_hash = hashlib.sha256()
                    total_bytes = 0
                    _logger.info("Fetching remote URI %s to %s", self.uri, final_path)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        content_hash.update(chunk)
                        total_bytes += len(chunk)

            os.replace(temp_path, final_path)
        except Exception as exc:  # noqa: BLE001
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            _logger.error("Failed to fetch remote URI %s: %s", self.uri, exc)
            raise HttpAssetError(f"Failed to fetch remote URI {self.uri!r}: {exc}") from exc

        now = _utc_now()
        manifest = HttpAssetManifest(
            request=HttpAssetManifestRequest(
                uri=self.uri,
                cache_key=cache_key,
                resolved_url=_optional_text(metadata.get("url")) or resolved_url,
            ),
            remote=HttpAssetManifestRemote(
                digest=_optional_text(metadata.get("digest")),
                digest_type=_optional_text(metadata.get("digest_type")),
                length=_optional_int(metadata.get("length")) or total_bytes,
                update_time=_optional_text(metadata.get("update_time")),
                metadata_checked_at=now,
            ),
            local=HttpAssetManifestLocal(
                payload_relpath=final_path.name,
                content_hash=content_hash.hexdigest(),
                size_bytes=total_bytes,
                downloaded_at=now,
            ),
        )
        write_manifest(manifest_path, manifest)
        _logger.info("Staged remote URI %s at %s (%d bytes)", self.uri, final_path, total_bytes)
        return MaterializedAsset(
            path=final_path,
            content_hash=content_hash.hexdigest(),
            metadata=_asset_metadata_from_manifest(manifest),
        )


def _suffix_for_url(uri: str) -> str:
    return Path(urlparse(uri).path).suffix


def _asset_metadata_from_manifest(manifest: HttpAssetManifest) -> AssetMetadata:
    return AssetMetadata(
        uri=manifest.request.uri,
        resolved_url=manifest.request.resolved_url,
        digest=manifest.remote.digest,
        digest_type=manifest.remote.digest_type,
        length=manifest.remote.length,
        update_time=manifest.remote.update_time,
        etag=manifest.remote.etag,
        last_modified=manifest.remote.last_modified,
        fetched_at=manifest.local.downloaded_at,
        cache_key=manifest.request.cache_key,
        transport=manifest.transport,
        extra=dict(manifest.extra),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
