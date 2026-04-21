"""Tests for Framera YOLOv26 frame schema helpers."""

from __future__ import annotations

import json

from mlody.teams.framera.yolo26.schema import Detection, FramePacket


def test_frame_packet_serializes_detections() -> None:
    packet = FramePacket(
        timestamp_ms=1234,
        frame_width=640,
        frame_height=480,
        task="detection",
        status="ok",
        model="yolo26x.pt",
        detections=(
            Detection(
                index=0,
                class_id=0,
                class_name="person",
                confidence=0.92,
                x1=1.0,
                y1=2.0,
                x2=10.0,
                y2=20.0,
            ),
        ),
    )

    payload = json.loads(packet.to_json_line())

    assert payload["task"] == "detection"
    assert payload["status"] == "ok"
    assert payload["model"] == "yolo26x.pt"
    assert payload["detections"][0]["class_name"] == "person"
    assert payload["detections"][0]["confidence"] == 0.92


def test_frame_packet_allows_empty_detection_set() -> None:
    packet = FramePacket(
        timestamp_ms=99,
        frame_width=320,
        frame_height=240,
        task="segmentation",
        status="no_detection",
        model="yolo26x-seg.pt",
        detections=(),
    )

    payload = json.loads(packet.to_json_line())

    assert payload["task"] == "segmentation"
    assert payload["status"] == "no_detection"
    assert payload["detections"] == []
