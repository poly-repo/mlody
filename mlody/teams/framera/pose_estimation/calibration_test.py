"""Tests for framera camera calibration loading."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mlody.teams.framera.pose_estimation.calibration import CameraCalibration, load_calibration


def _write_json_calibration(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "camera_matrix": [
                    [900.0, 0.0, 320.0],
                    [0.0, 900.0, 240.0],
                    [0.0, 0.0, 1.0],
                ],
                "distortion_coefficients": [0.1, -0.2, 0.0, 0.0, 0.0],
                "image_width": 640,
                "image_height": 480,
            }
        )
    )


def test_load_calibration_reads_json_intrinsics(tmp_path: Path) -> None:
    calibration_path = tmp_path / "camera.json"
    _write_json_calibration(calibration_path)

    calibration = load_calibration(calibration_path)

    assert isinstance(calibration, CameraCalibration)
    assert calibration.image_width == 640
    assert calibration.image_height == 480
    assert calibration.camera_matrix.shape == (3, 3)
    assert np.isclose(calibration.camera_matrix[0, 0], 900.0)
    assert calibration.distortion_coefficients.shape == (5,)


def test_validate_capture_size_raises_on_mismatch(tmp_path: Path) -> None:
    calibration_path = tmp_path / "camera.json"
    _write_json_calibration(calibration_path)
    calibration = load_calibration(calibration_path)

    with pytest.raises(ValueError, match="capture size"):
        calibration.validate_capture_size(width=1280, height=720)
