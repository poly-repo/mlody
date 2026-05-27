"""Persistent HTTP-backed asset source."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from mlody.db.assets import LatestObservation

from common.python.http_info import fetch_http_info
from mlody.core.assets.cache import (
    cache_dir_for_key,
    default_http_cache_root,
    ensure_cache_root,
)
from mlody.core.assets.freshness_policy import (
    manifest_with_refreshed_remote_metadata,
    remote_metadata_indicates_change,
    should_revalidate_http_asset,
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
    freshness: object | None = None
    db_conn: sqlite3.Connection | None = None
    asset_id: str | None = None

    def materialize(self) -> MaterializedAsset:
        """Return a locally cached copy of *uri*."""
        parsed = urlparse(self.uri)
        if parsed.scheme not in {"http", "https"}:
            raise HttpAssetError(
                f"https(uri=...) only supports http/https in v1, got {parsed.scheme!r}"
            )

        if self.db_conn is not None and self.asset_id is not None:
            return self._materialize_with_db()

        cache_root = self.cache_root or default_http_cache_root()
        ensure_cache_root(cache_root)
        cache_key = cache_key_for_uri(self.uri)
        cache_dir = cache_dir_for_key(cache_root, cache_key)
        manifest_path = cache_dir / "manifest.json"

        manifest = self._load_manifest(manifest_path)
        cached_asset = self._cached_asset_from_manifest(manifest_path, manifest)
        if cached_asset is not None and not should_revalidate_http_asset(
            self.freshness,
            manifest,
        ):
            _logger.info("Reusing cached remote URI %s from %s", self.uri, cached_asset.path)
            return cached_asset

        if cached_asset is not None and manifest is not None:
            revalidated = self._revalidate_cached_asset(
                cache_dir,
                manifest_path,
                cache_key,
                manifest,
                cached_asset,
            )
            if revalidated is not None:
                return revalidated

        return self._download_asset(cache_dir, manifest_path, cache_key)

    def _load_manifest(self, manifest_path: Path) -> HttpAssetManifest | None:
        if not manifest_path.exists():
            return None

        try:
            return load_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Ignoring unreadable remote asset manifest at %s: %s", manifest_path, exc)
            return None

    def _cached_asset_from_manifest(
        self,
        manifest_path: Path,
        manifest: HttpAssetManifest | None,
    ) -> MaterializedAsset | None:
        if manifest is None:
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

    def _revalidate_cached_asset(
        self,
        cache_dir: Path,
        manifest_path: Path,
        cache_key: str,
        manifest: HttpAssetManifest,
        cached_asset: MaterializedAsset,
    ) -> MaterializedAsset | None:
        try:
            metadata = fetch_http_info(self.uri)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Unable to revalidate cached remote URI %s; reusing cached payload",
                self.uri,
                exc_info=True,
            )
            return cached_asset

        if remote_metadata_indicates_change(manifest, metadata):
            return self._download_asset(
                cache_dir,
                manifest_path,
                cache_key,
                metadata=metadata,
            )

        refreshed_manifest = manifest_with_refreshed_remote_metadata(
            manifest,
            metadata,
            checked_at=_utc_now(),
        )
        write_manifest(manifest_path, refreshed_manifest)
        _logger.info(
            "Revalidated cached remote URI %s without downloading new bytes",
            self.uri,
        )
        return MaterializedAsset(
            path=cached_asset.path,
            content_hash=cached_asset.content_hash,
            metadata=_asset_metadata_from_manifest(refreshed_manifest),
        )

    def _download_asset(
        self,
        cache_dir: Path,
        manifest_path: Path,
        cache_key: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> MaterializedAsset:
        final_path, content_hash_hex, total_bytes, resolved_url, metadata = (
            self._fetch_bytes(cache_dir, metadata=metadata)
        )
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
                etag=_optional_text(metadata.get("etag")),
                last_modified=_optional_text(metadata.get("last_modified")),
                metadata_checked_at=now,
            ),
            local=HttpAssetManifestLocal(
                payload_relpath=final_path.name,
                content_hash=content_hash_hex,
                size_bytes=total_bytes,
                downloaded_at=now,
            ),
        )
        write_manifest(manifest_path, manifest)
        _logger.info("Staged remote URI %s at %s (%d bytes)", self.uri, final_path, total_bytes)
        return MaterializedAsset(
            path=final_path,
            content_hash=content_hash_hex,
            metadata=_asset_metadata_from_manifest(manifest),
        )

    def _fetch_bytes(
        self,
        cache_dir: Path,
        *,
        metadata: dict[str, object] | None = None,
    ) -> tuple[Path, str, int, str, dict[str, object]]:
        """Download *uri* into *cache_dir*; return (path, content_hash, bytes, resolved_url, metadata)."""
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if metadata is None:
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
        return final_path, content_hash.hexdigest(), total_bytes, resolved_url, metadata

    # ------------------------------------------------------------------
    # DB-backed path (active when db_conn and asset_id are both set)
    # ------------------------------------------------------------------

    def _materialize_with_db(self) -> MaterializedAsset:
        from mlody.db.assets import latest_observation, record_observation, upsert_blob  # noqa: PLC0415
        from mlody.core.assets.freshness_policy import (  # noqa: PLC0415
            metadata_indicates_change,
            should_revalidate_from_timestamp,
        )

        cache_key = cache_key_for_uri(self.uri)
        obs = latest_observation(self.db_conn, cache_key)
        cached = self._cached_asset_from_observation(obs)

        if cached is not None and not should_revalidate_from_timestamp(
            self.freshness, obs.observed_at if obs is not None else None
        ):
            _logger.info("Reusing cached remote URI %s (DB)", self.uri)
            return cached

        if cached is not None and obs is not None:
            try:
                remote_meta = fetch_http_info(self.uri)
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Unable to revalidate cached remote URI %s; reusing cached payload",
                    self.uri,
                    exc_info=True,
                )
                return cached

            if not metadata_indicates_change(
                remote_meta,
                known_digest=obs.remote_digest,
                known_update_time=obs.update_time,
                known_length=obs.content_length,
            ):
                record_observation(
                    self.db_conn,
                    asset_id=self.asset_id,
                    blob_sha=obs.blob_sha,
                    status="revalidated",
                    etag=_optional_text(remote_meta.get("etag")),
                    last_modified=_optional_text(remote_meta.get("last_modified")),
                    remote_digest=_optional_text(remote_meta.get("digest")),
                    remote_digest_type=_optional_text(remote_meta.get("digest_type")),
                    content_length=_optional_int(remote_meta.get("length")),
                    update_time=_optional_text(remote_meta.get("update_time")),
                    resolved_url=_optional_text(remote_meta.get("url")),
                )
                _logger.info("Revalidated remote URI %s without re-download (DB)", self.uri)
                return cached

        return self._download_asset_db(cache_key)

    def _cached_asset_from_observation(self, obs: LatestObservation | None) -> MaterializedAsset | None:
        if obs is None:
            return None
        blob_path = Path(obs.local_path)
        if not blob_path.exists():
            return None
        return MaterializedAsset(
            path=blob_path,
            content_hash=obs.blob_sha,
            metadata=AssetMetadata(
                uri=self.uri,
                resolved_url=obs.resolved_url,
                digest=obs.remote_digest,
                digest_type=obs.remote_digest_type,
                length=obs.content_length,
                update_time=obs.update_time,
                etag=obs.etag,
                last_modified=obs.last_modified,
                fetched_at=obs.observed_at,
                cache_key=cache_key_for_uri(self.uri),
                transport="http",
            ),
        )

    def _download_asset_db(self, cache_key: str) -> MaterializedAsset:
        from mlody.db.assets import record_observation, upsert_blob  # noqa: PLC0415

        cache_root = self.cache_root or default_http_cache_root()
        ensure_cache_root(cache_root)
        cache_dir = cache_dir_for_key(cache_root, cache_key)
        final_path, content_hash_hex, total_bytes, resolved_url, metadata = (
            self._fetch_bytes(cache_dir)
        )
        upsert_blob(
            self.db_conn,
            content_hash=content_hash_hex,
            local_path=str(final_path),
            size_bytes=total_bytes,
        )
        record_observation(
            self.db_conn,
            asset_id=self.asset_id,
            blob_sha=content_hash_hex,
            status="downloaded",
            etag=_optional_text(metadata.get("etag")),
            last_modified=_optional_text(metadata.get("last_modified")),
            remote_digest=_optional_text(metadata.get("digest")),
            remote_digest_type=_optional_text(metadata.get("digest_type")),
            content_length=_optional_int(metadata.get("length")) or total_bytes,
            update_time=_optional_text(metadata.get("update_time")),
            resolved_url=_optional_text(metadata.get("url")) or resolved_url,
        )
        _logger.info("Staged remote URI %s at %s (%d bytes, DB)", self.uri, final_path, total_bytes)
        return MaterializedAsset(
            path=final_path,
            content_hash=content_hash_hex,
            metadata=AssetMetadata(
                uri=self.uri,
                resolved_url=_optional_text(metadata.get("url")) or resolved_url,
                digest=_optional_text(metadata.get("digest")),
                digest_type=_optional_text(metadata.get("digest_type")),
                length=_optional_int(metadata.get("length")) or total_bytes,
                update_time=_optional_text(metadata.get("update_time")),
                etag=_optional_text(metadata.get("etag")),
                last_modified=_optional_text(metadata.get("last_modified")),
                fetched_at=_utc_now(),
                cache_key=cache_key,
                transport="http",
            ),
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
