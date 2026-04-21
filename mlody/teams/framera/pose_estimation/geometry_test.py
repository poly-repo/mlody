"""Tests for framera geometry helpers."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np

from mlody.teams.framera.pose_estimation import geometry as geometry_module
from mlody.teams.framera.pose_estimation.calibration import CameraCalibration
from mlody.teams.framera.pose_estimation.geometry import (
    reconstruct_face_landmarks_camera_space,
    reconstruct_hand_landmarks_camera_space,
    reconstruct_pose_landmarks_camera_space,
)
from mlody.teams.framera.pose_estimation.mediapipe_adapter import NormalizedLandmark


def _calibration() -> CameraCalibration:
    return CameraCalibration(
        camera_matrix=np.array(
            [
                [900.0, 0.0, 320.0],
                [0.0, 900.0, 240.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        ),
        distortion_coefficients=np.zeros(5, dtype=np.float64),
        image_width=640,
        image_height=480,
        source_path="memory://camera.json",
        source_format="json",
    )


def test_reconstruct_pose_marks_frame_degraded_without_enough_points() -> None:
    pose, degraded, warnings = reconstruct_pose_landmarks_camera_space(
        calibration=_calibration(),
        pose_landmarks=(NormalizedLandmark(index=0, x=0.5, y=0.5, z=0.1, visibility=0.9),),
        pose_world_landmarks=(
            NormalizedLandmark(index=0, x=0.0, y=0.0, z=0.0, visibility=0.9),
        ),
    )

    assert pose == ()
    assert degraded is True
    assert "pose_pnp_requires_four_points" in warnings


def test_reconstruct_face_uses_selected_anchor_points() -> None:
    face_landmarks = tuple(
        NormalizedLandmark(index=index, x=0.4 + index * 0.001, y=0.3, z=0.01, visibility=0.9)
        for index in (1, 33, 61, 199, 263, 291)
    )

    fake_cv2 = type(
        "FakeCv2",
        (),
        {
            "SOLVEPNP_ITERATIVE": 0,
            "solvePnP": staticmethod(lambda *args, **kwargs: (True, np.zeros((3, 1)), np.array([[0.0], [0.0], [1.0]]))),
        },
    )

    with patch.object(geometry_module, "_load_cv2", return_value=fake_cv2):
        face, degraded, warnings = reconstruct_face_landmarks_camera_space(
            calibration=_calibration(),
            face_landmarks=face_landmarks,
        )

    assert len(face) == len(face_landmarks)
    assert degraded is False
    assert warnings == ()


def test_reconstruct_hand_uses_world_landmarks_when_available() -> None:
    fake_cv2 = type(
        "FakeCv2",
        (),
        {
            "SOLVEPNP_EPNP": 0,
            "solvePnP": staticmethod(
                lambda *args, **kwargs: (
                    True,
                    np.zeros((3, 1)),
                    np.array([[0.0], [0.0], [0.5]]),
                )
            ),
            "Rodrigues": staticmethod(lambda _rvec: (np.eye(3), None)),
        },
    )
    hand_landmarks = tuple(
        NormalizedLandmark(index=index, x=0.4, y=0.3 + index * 0.01, z=0.01, visibility=0.9)
        for index in range(4)
    )
    hand_world_landmarks = tuple(
        NormalizedLandmark(index=index, x=index * 0.01, y=0.0, z=0.0, visibility=0.9)
        for index in range(4)
    )

    with patch.object(geometry_module, "_load_cv2", return_value=fake_cv2):
        hand, degraded, warnings = reconstruct_hand_landmarks_camera_space(
            calibration=_calibration(),
            hand_landmarks=hand_landmarks,
            hand_world_landmarks=hand_world_landmarks,
            hand_label="left",
        )

    assert len(hand) == 4
    assert degraded is False
    assert warnings == ()
