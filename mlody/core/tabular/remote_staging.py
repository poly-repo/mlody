"""Per-process staging of remote files for tabular consumers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mlody.core.assets.http_asset import HttpAssetError, HttpAssetSource

_logger = logging.getLogger(__name__)


class RemoteFetchError(ValueError):
    """Raised when a remote URI cannot be staged for local access."""


@dataclass(frozen=True)
class StagedRemoteFile:
    """A remote file materialized into the persistent asset cache."""

    uri: str
    path: Path
    content_hash: str


class RemoteStagingManager:
    """Cache remote files in a persistent asset cache with per-process memoization."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self._cache_root = cache_root
        self._staged: dict[str, StagedRemoteFile] = {}

    def stage(self, uri: str) -> StagedRemoteFile:
        """Stage *uri* locally and return the cached local artifact."""
        if uri in self._staged:
            _logger.debug("Remote staging cache hit for %s", uri)
            return self._staged[uri]

        try:
            materialized = HttpAssetSource(uri=uri, cache_root=self._cache_root).materialize()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, HttpAssetError):
                raise RemoteFetchError(str(exc)) from exc
            _logger.error("Failed to stage remote URI %s: %s", uri, exc)
            raise RemoteFetchError(f"Failed to stage remote URI {uri!r}: {exc}") from exc

        staged = StagedRemoteFile(
            uri=uri,
            path=materialized.path,
            content_hash=materialized.content_hash or "",
        )
        self._staged[uri] = staged
        return staged


_REMOTE_STAGING_MANAGER = RemoteStagingManager()


def stage_remote_file(uri: str) -> StagedRemoteFile:
    """Stage *uri* via the process-global remote staging manager."""
    return _REMOTE_STAGING_MANAGER.stage(uri)
