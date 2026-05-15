"""Core asset abstractions shared by transport-specific source types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mlody.core.assets.metadata import AssetMetadata


@dataclass(frozen=True, slots=True)
class MaterializedAsset:
    """A concrete local artifact plus the metadata known about its origin."""

    path: Path
    content_hash: str | None
    metadata: AssetMetadata


class AssetSource(Protocol):
    """Protocol for sources that can materialize an artifact locally."""

    def materialize(self) -> MaterializedAsset:
        """Materialize the source and return the local artifact."""

