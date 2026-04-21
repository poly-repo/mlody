"""Tests for framera frame schema helpers."""

from __future__ import annotations

import json
from pathlib import Path

from mlody.teams.framera.pose_estimation.schema import FramePacket, Landmark3D


def test_frame_packet_serializes_status_and_landmarks() -> None:
    packet = FramePacket(
        timestamp_ms=1234,
        frame_width=640,
        frame_height=480,
        status="ok",
        degraded=False,
        metric_3d=True,
        warnings=("none",),
        calibration={"image_width": 640, "image_height": 480},
        face=(
            Landmark3D(index=1, x=0.1, y=0.2, z=0.3, visibility=0.9, presence=None),
        ),
        pose=(
            Landmark3D(index=0, x=1.0, y=2.0, z=3.0, visibility=0.8, presence=0.7),
        ),
        left_hand=(
            Landmark3D(index=5, x=4.0, y=5.0, z=6.0, visibility=0.7, presence=0.6),
        ),
        right_hand=(),
    )

    payload = json.loads(packet.to_json_line())

    assert payload["status"] == "ok"
    assert payload["degraded"] is False
    assert payload["metric_3d"] is True
    assert payload["face"][0]["index"] == 1
    assert payload["pose"][0]["presence"] == 0.7
    assert payload["left_hand"][0]["index"] == 5
    assert payload["right_hand"] == []


def test_frame_packet_allows_empty_landmark_sets() -> None:
    packet = FramePacket(
        timestamp_ms=99,
        frame_width=320,
        frame_height=240,
        status="no_detection",
        degraded=True,
        metric_3d=False,
        warnings=("missing_landmarks",),
        calibration={"image_width": 320, "image_height": 240},
        face=(),
        pose=(),
        left_hand=(),
        right_hand=(),
    )

    payload = json.loads(packet.to_json_line())

    assert payload["status"] == "no_detection"
    assert payload["face"] == []
    assert payload["pose"] == []
    assert payload["left_hand"] == []
    assert payload["right_hand"] == []
