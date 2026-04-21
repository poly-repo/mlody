"""JSONL frame contracts for the framera pose estimator."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Landmark3D:
    """One reconstructed landmark in camera space."""

    index: int
    x: float
    y: float
    z: float
    visibility: float | None
    presence: float | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe landmark payload."""
        payload: dict[str, object] = {
            "index": self.index,
            "x": self.x,
            "y": self.y,
            "z": self.z,
        }
        if self.visibility is not None:
            payload["visibility"] = self.visibility
        if self.presence is not None:
            payload["presence"] = self.presence
        return payload


@dataclass(frozen=True)
class FramePacket:
    """One emitted stdout frame."""

    timestamp_ms: int
    frame_width: int
    frame_height: int
    status: str
    degraded: bool
    metric_3d: bool
    warnings: tuple[str, ...]
    calibration: dict[str, object]
    face: tuple[Landmark3D, ...]
    pose: tuple[Landmark3D, ...]
    left_hand: tuple[Landmark3D, ...]
    right_hand: tuple[Landmark3D, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the packet as a JSON-safe mapping."""
        return {
            "timestamp_ms": self.timestamp_ms,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "status": self.status,
            "degraded": self.degraded,
            "metric_3d": self.metric_3d,
            "warnings": list(self.warnings),
            "calibration": self.calibration,
            "face": [landmark.to_dict() for landmark in self.face],
            "pose": [landmark.to_dict() for landmark in self.pose],
            "left_hand": [landmark.to_dict() for landmark in self.left_hand],
            "right_hand": [landmark.to_dict() for landmark in self.right_hand],
        }

    def to_json_line(self) -> str:
        """Serialize the packet as one JSONL line."""
        return json.dumps(self.to_dict(), separators=(",", ":"))
