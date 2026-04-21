"""Tests for framera runtime helpers."""

from __future__ import annotations

from mlody.teams.framera.pose_estimation.runtime import (
    EmissionGate,
    FpsCounter,
    build_overlay_lines,
    choose_status,
)


def test_emission_gate_allows_first_emit_and_respects_interval() -> None:
    gate = EmissionGate(interval_seconds=0.5)

    assert gate.should_emit(0.0) is True
    assert gate.should_emit(0.2) is False
    assert gate.should_emit(0.6) is True


def test_choose_status_prefers_no_detection_then_degraded() -> None:
    assert choose_status(face_count=0, pose_count=0, hand_count=0, degraded=False) == "no_detection"
    assert choose_status(face_count=1, pose_count=0, hand_count=0, degraded=True) == "degraded"
    assert choose_status(face_count=0, pose_count=0, hand_count=1, degraded=False) == "ok"


def test_fps_counter_smooths_frame_rate_updates() -> None:
    counter = FpsCounter()

    assert counter.update(0.0) == 0.0
    assert round(counter.update(0.1), 1) == 10.0
    assert round(counter.update(0.2), 1) == 10.0


def test_build_overlay_lines_includes_status_and_fps() -> None:
    assert build_overlay_lines(status="ok", fps=12.34) == ("Status: ok", "FPS: 12.3")
