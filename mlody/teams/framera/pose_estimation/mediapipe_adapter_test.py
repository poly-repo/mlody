"""Tests for MediaPipe adapter compatibility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mlody.teams.framera.pose_estimation import mediapipe_adapter as mediapipe_adapter_module
from mlody.teams.framera.pose_estimation.mediapipe_adapter import (
    MediaPipeTracker,
    _try_load_mediapipe_solutions,
)


def test_try_load_mediapipe_solutions_uses_top_level_module_when_available() -> None:
    fake_solutions = SimpleNamespace(name="top-level")
    fake_mp = SimpleNamespace(solutions=fake_solutions)

    with patch.object(mediapipe_adapter_module, "_import_mediapipe", return_value=fake_mp):
        solutions = _try_load_mediapipe_solutions()

    assert solutions is fake_solutions


def test_try_load_mediapipe_solutions_returns_none_when_missing() -> None:
    fake_mp = SimpleNamespace()

    with patch.object(mediapipe_adapter_module, "_import_mediapipe", return_value=fake_mp):
        solutions = _try_load_mediapipe_solutions()

    assert solutions is None


def test_tracker_raises_clear_error_when_tasks_only_and_model_missing() -> None:
    tracker = MediaPipeTracker()

    with patch.object(
        mediapipe_adapter_module,
        "_try_load_mediapipe_solutions",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="--holistic-model"):
            tracker.__enter__()


def test_tracker_requires_hand_model_for_split_tasks_hands() -> None:
    tracker = MediaPipeTracker(
        face_model_path="face.task",
        hands_enabled=True,
    )

    with patch.object(
        mediapipe_adapter_module,
        "_try_load_mediapipe_solutions",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="--hand-model"):
            tracker.__enter__()


def test_tracker_requires_pose_model_when_body_enabled() -> None:
    tracker = MediaPipeTracker(
        face_model_path="face.task",
        body_enabled=True,
    )

    with patch.object(
        mediapipe_adapter_module,
        "_try_load_mediapipe_solutions",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="--pose-model"):
            tracker.__enter__()


def test_tracker_ignores_unused_pose_and_hand_models_without_flags() -> None:
    tracker = MediaPipeTracker(
        face_model_path="face.task",
        pose_model_path="pose.task",
        hand_model_path="hand.task",
        body_enabled=False,
        hands_enabled=False,
    )

    with (
        patch.object(mediapipe_adapter_module, "_try_load_mediapipe_solutions", return_value=None),
        patch.object(mediapipe_adapter_module, "_import_mediapipe", return_value=object()),
        patch.object(mediapipe_adapter_module, "_create_task_face_landmarker", return_value=object()) as face_mock,
        patch.object(mediapipe_adapter_module, "_create_task_pose_landmarker") as pose_mock,
        patch.object(mediapipe_adapter_module, "_create_task_hand_landmarker") as hand_mock,
    ):
        entered = tracker.__enter__()

    assert entered is tracker
    face_mock.assert_called_once()
    pose_mock.assert_not_called()
    hand_mock.assert_not_called()
