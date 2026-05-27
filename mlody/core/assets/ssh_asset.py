"""SSH-backed asset sources staged in the mlody cache."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.location_specs import SshLocationSpec


class SshAssetError(ValueError):
    """Raised when an SSH-backed asset cannot be materialized."""


@dataclass(frozen=True, slots=True)
class SshAssetSource:
    """Read a staged SSH artifact from the mlody cache."""

    host: str
    remote_path: str
    cache_root: Path | None = None

    def materialize(self) -> MaterializedAsset:
        """Return the staged local artifact represented by this SSH location."""
        spec = SshLocationSpec(host=self.host, path=self.remote_path)
        path = spec.cache_path(cache_root=self.cache_root)
        if not path.exists():
            raise SshAssetError(
                f"SSH asset cache path does not exist: {path} "
                f"(host={self.host!r}, path={self.remote_path!r})"
            )
        uri = self._uri()
        return MaterializedAsset(
            path=path,
            content_hash=self._file_content_hash(path),
            metadata=AssetMetadata(
                uri=uri,
                resolved_url=uri,
                digest=None,
                digest_type=None,
                length=path.stat().st_size,
                update_time=None,
                cache_key=None,
                transport="ssh",
                extra={
                    "host": self.host,
                    "path": self.remote_path,
                    "cache_path": str(path),
                },
            ),
        )

    def _uri(self) -> str:
        normalized_path = (
            self.remote_path
            if self.remote_path.startswith("/")
            else "/" + self.remote_path
        )
        return f"ssh://{self.host}{normalized_path}"

    @staticmethod
    def _file_content_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


__all__ = ["SshAssetError", "SshAssetSource"]
