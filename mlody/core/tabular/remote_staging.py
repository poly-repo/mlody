"""Per-process staging of remote files for tabular consumers."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_logger = logging.getLogger(__name__)


class RemoteFetchError(ValueError):
    """Raised when a remote URI cannot be staged for local access."""


@dataclass(frozen=True)
class StagedRemoteFile:
    """A remote file materialized into the process-local temp directory."""

    uri: str
    path: Path
    content_hash: str


class RemoteStagingManager:
    """Download remote files once per process into a private temp directory."""

    def __init__(self) -> None:
        self._tmpdir = TemporaryDirectory(prefix="mlody-remote-")
        self._staged: dict[str, StagedRemoteFile] = {}

    def stage(self, uri: str) -> StagedRemoteFile:
        """Stage *uri* locally and return the cached local artifact."""
        if uri in self._staged:
            _logger.debug("Remote staging cache hit for %s", uri)
            return self._staged[uri]

        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https"}:
            raise RemoteFetchError(
                f"remote(uri=...) only supports http/https in v1, got {parsed.scheme!r}"
            )

        suffix = Path(parsed.path).suffix
        name_digest = hashlib.sha256(uri.encode()).hexdigest()[:16]
        dest = Path(self._tmpdir.name) / f"{name_digest}{suffix}"
        request = Request(uri, headers={"User-Agent": "mlody/remote"})
        content_hash = hashlib.sha256()
        total_bytes = 0
        _logger.info("Fetching remote URI %s to %s", uri, dest)
        try:
            with urlopen(request) as response, dest.open("wb") as handle:  # noqa: S310
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    content_hash.update(chunk)
                    total_bytes += len(chunk)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Failed to fetch remote URI %s: %s", uri, exc)
            raise RemoteFetchError(f"Failed to fetch remote URI {uri!r}: {exc}") from exc

        staged = StagedRemoteFile(
            uri=uri,
            path=dest,
            content_hash=content_hash.hexdigest(),
        )
        self._staged[uri] = staged
        _logger.info(
            "Staged remote URI %s at %s (%d bytes)",
            uri,
            dest,
            total_bytes,
        )
        return staged


_REMOTE_STAGING_MANAGER = RemoteStagingManager()


def stage_remote_file(uri: str) -> StagedRemoteFile:
    """Stage *uri* via the process-global remote staging manager."""
    return _REMOTE_STAGING_MANAGER.stage(uri)
