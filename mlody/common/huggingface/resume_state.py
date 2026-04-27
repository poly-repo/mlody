"""Resume-state management for segmented Hugging Face downloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PartialMetadata:
    """Validated metadata for a partially downloaded file."""

    size: int
    segment_size: int
    completed_segments: tuple[bool, ...]

    @property
    def segment_count(self) -> int:
        """Return the number of expected segments."""
        return len(self.completed_segments)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON payload stored next to the partial file."""
        return {
            "size": self.size,
            "segment_size": self.segment_size,
            "segment_count": self.segment_count,
            "completed_segments": list(self.completed_segments),
        }

    def mark_completed(self, index: int) -> PartialMetadata:
        """Return a new state with one segment marked complete."""
        completed_segments = list(self.completed_segments)
        completed_segments[index] = True
        return PartialMetadata(
            size=self.size,
            segment_size=self.segment_size,
            completed_segments=tuple(completed_segments),
        )


def partial_path(path: Path) -> Path:
    """Return the sidecar partial-file path used during download."""
    return path.with_name(f"{path.name}.partial")


def partial_metadata_path(path: Path) -> Path:
    """Return the sidecar metadata path for a partial download."""
    return path.with_name(f"{path.name}.metadata.json")


def load_partial_metadata(
    path: Path,
    size: int,
    segment_count: int,
    *,
    segment_size: int,
) -> PartialMetadata | None:
    """Load and validate persisted partial-download metadata."""
    metadata_path = partial_metadata_path(path)
    if not path.exists() or not metadata_path.exists():
        return None

    try:
        with metadata_path.open() as handle:
            raw_metadata = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(raw_metadata, dict):
        return None
    if (
        raw_metadata.get("size") != size
        or raw_metadata.get("segment_size") != segment_size
        or raw_metadata.get("segment_count") != segment_count
    ):
        return None

    completed = raw_metadata.get("completed_segments")
    if not isinstance(completed, list) or len(completed) != segment_count:
        return None
    if not all(isinstance(value, bool) for value in completed):
        return None

    return PartialMetadata(
        size=size,
        segment_size=segment_size,
        completed_segments=tuple(completed),
    )


def write_partial_metadata(path: Path, metadata: PartialMetadata) -> None:
    """Persist validated partial-download metadata."""
    with partial_metadata_path(path).open("w") as handle:
        json.dump(metadata.as_dict(), handle)


def initialize_partial_state(
    path: Path,
    size: int,
    segment_count: int,
    *,
    segment_size: int,
) -> PartialMetadata:
    """Create an empty partial file and matching metadata."""
    with path.open("wb") as handle:
        handle.truncate(size)

    metadata = PartialMetadata(
        size=size,
        segment_size=segment_size,
        completed_segments=tuple(False for _ in range(segment_count)),
    )
    write_partial_metadata(path, metadata)
    return metadata


def mark_segment_completed(
    path: Path,
    metadata: PartialMetadata,
    index: int,
) -> PartialMetadata:
    """Persist one completed segment and return the updated state."""
    updated = metadata.mark_completed(index)
    write_partial_metadata(path, updated)
    return updated
