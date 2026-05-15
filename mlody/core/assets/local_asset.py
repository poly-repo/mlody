"""Local filesystem-backed asset sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata


class LocalAssetError(ValueError):
    """Raised when a local asset cannot be materialized."""


@dataclass(frozen=True, slots=True)
class LocalPathAssetSource:
    """An asset source that points at an existing local filesystem path."""

    path: Path

    def materialize(self) -> MaterializedAsset:
        """Return the local artifact represented by ``self.path``."""
        if not self.path.exists():
            raise LocalAssetError(f"Local asset path does not exist: {self.path}")
        return MaterializedAsset(
            path=self.path,
            content_hash=None,
            metadata=AssetMetadata(
                uri=None,
                resolved_url=None,
                digest=None,
                digest_type=None,
                length=None,
                update_time=None,
                cache_key=None,
                transport="posix",
                extra={"path": str(self.path)},
            ),
        )

