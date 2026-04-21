"""Camera-space reconstruction helpers for framera."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt

from .calibration import CameraCalibration
from .mediapipe_adapter import NormalizedLandmark
from .schema import Landmark3D


_FACE_ANCHOR_MODEL_METERS: dict[int, tuple[float, float, float]] = {
    1: (0.0, 0.0, 0.0),
    33: (-0.03, 0.02, -0.02),
    61: (-0.025, -0.025, -0.015),
    199: (0.0, -0.06, -0.01),
    263: (0.03, 0.02, -0.02),
    291: (0.025, -0.025, -0.015),
}


def reconstruct_pose_landmarks_camera_space(
    *,
    calibration: CameraCalibration,
    pose_landmarks: tuple[NormalizedLandmark, ...],
    pose_world_landmarks: tuple[NormalizedLandmark, ...],
) -> tuple[tuple[Landmark3D, ...], bool, tuple[str, ...]]:
    """Convert pose world landmarks into camera-space coordinates."""
    return _reconstruct_world_landmarks_camera_space(
        calibration=calibration,
        image_landmarks=pose_landmarks,
        world_landmarks=pose_world_landmarks,
        missing_warning="pose_pnp_requires_four_points",
        failed_warning="pose_pnp_failed",
    )


def reconstruct_hand_landmarks_camera_space(
    *,
    calibration: CameraCalibration,
    hand_landmarks: tuple[NormalizedLandmark, ...],
    hand_world_landmarks: tuple[NormalizedLandmark, ...],
    hand_label: str,
) -> tuple[tuple[Landmark3D, ...], bool, tuple[str, ...]]:
    """Convert hand world landmarks into camera-space coordinates."""
    return _reconstruct_world_landmarks_camera_space(
        calibration=calibration,
        image_landmarks=hand_landmarks,
        world_landmarks=hand_world_landmarks,
        missing_warning=f"{hand_label}_hand_pnp_requires_four_points",
        failed_warning=f"{hand_label}_hand_pnp_failed",
    )


def reconstruct_face_landmarks_camera_space(
    *,
    calibration: CameraCalibration,
    face_landmarks: tuple[NormalizedLandmark, ...],
) -> tuple[tuple[Landmark3D, ...], bool, tuple[str, ...]]:
    """Approximate camera-space face landmarks using calibrated head pose."""
    landmark_map = {landmark.index: landmark for landmark in face_landmarks}
    available_indices = [index for index in _FACE_ANCHOR_MODEL_METERS if index in landmark_map]
    if len(available_indices) < 4:
        return (), True, ("face_anchor_points_missing",)

    cv2 = _load_cv2()
    object_points = np.asarray(
        [_FACE_ANCHOR_MODEL_METERS[index] for index in available_indices],
        dtype=np.float64,
    )
    image_points = np.asarray(
        [
            [
                landmark_map[index].x * calibration.image_width,
                landmark_map[index].y * calibration.image_height,
            ]
            for index in available_indices
        ],
        dtype=np.float64,
    )
    success, _, tvec = cv2.solvePnP(
        object_points,
        image_points,
        calibration.camera_matrix,
        calibration.distortion_coefficients,
        flags=getattr(cv2, "SOLVEPNP_ITERATIVE", 0),
    )
    if not success:
        return (), True, ("face_pnp_failed",)

    inverse_camera = np.linalg.inv(calibration.camera_matrix)
    reference_depth = max(float(tvec[2, 0]), 0.05)
    output: list[Landmark3D] = []
    for landmark in face_landmarks:
        pixel = np.asarray(
            [
                landmark.x * calibration.image_width,
                landmark.y * calibration.image_height,
                1.0,
            ],
            dtype=np.float64,
        )
        depth = max(reference_depth + landmark.z * 0.08, 0.05)
        camera_point = depth * (inverse_camera @ pixel)
        output.append(
            Landmark3D(
                index=landmark.index,
                x=float(camera_point[0]),
                y=float(camera_point[1]),
                z=float(camera_point[2]),
                visibility=landmark.visibility,
                presence=landmark.presence,
            )
        )
    return tuple(output), False, ()


def _pair_landmarks(
    image_landmarks: tuple[NormalizedLandmark, ...],
    world_landmarks: tuple[NormalizedLandmark, ...],
) -> list[tuple[NormalizedLandmark, NormalizedLandmark]]:
    image_by_index = {landmark.index: landmark for landmark in image_landmarks}
    world_by_index = {landmark.index: landmark for landmark in world_landmarks}
    shared_indices = sorted(set(image_by_index).intersection(world_by_index))
    return [(image_by_index[index], world_by_index[index]) for index in shared_indices]


def _reconstruct_world_landmarks_camera_space(
    *,
    calibration: CameraCalibration,
    image_landmarks: tuple[NormalizedLandmark, ...],
    world_landmarks: tuple[NormalizedLandmark, ...],
    missing_warning: str,
    failed_warning: str,
) -> tuple[tuple[Landmark3D, ...], bool, tuple[str, ...]]:
    paired = _pair_landmarks(image_landmarks, world_landmarks)
    if len(paired) < 4:
        return (), True, (missing_warning,)

    cv2 = _load_cv2()
    object_points = np.asarray(
        [[world.x, world.y, world.z] for _, world in paired],
        dtype=np.float64,
    )
    image_points = np.asarray(
        [
            [
                image.x * calibration.image_width,
                image.y * calibration.image_height,
            ]
            for image, _ in paired
        ],
        dtype=np.float64,
    )

    success, rvec, tvec = cv2.solvePnP(
        object_points,
        image_points,
        calibration.camera_matrix,
        calibration.distortion_coefficients,
        flags=getattr(cv2, "SOLVEPNP_EPNP", 0),
    )
    if not success:
        return (), True, (failed_warning,)
    rotation_matrix, _ = cv2.Rodrigues(rvec)
    camera_points = (rotation_matrix @ object_points.T).T + tvec.reshape(1, 3)
    output = tuple(
        Landmark3D(
            index=image.index,
            x=float(point[0]),
            y=float(point[1]),
            z=float(point[2]),
            visibility=image.visibility,
            presence=image.presence,
        )
        for (image, _), point in zip(paired, camera_points, strict=True)
    )
    return output, False, ()


def _load_cv2() -> Any:
    import cv2

    return cv2
