"""Tests for the framera camera calibration CLI and helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
from click.testing import CliRunner

from mlody.teams.framera.pose_estimation import camera_calibrate as camera_calibrate_module
from mlody.teams.framera.pose_estimation.calibration import CameraCalibration
from mlody.teams.framera.pose_estimation.camera_calibrate import (
    CalibrationResult,
    build_object_points,
    cli,
    write_calibration_json,
)


def test_build_object_points_uses_square_size_meters() -> None:
    points = build_object_points(cols=9, rows=6, square_size_m=0.025)

    assert points.shape == (54, 3)
    assert np.allclose(points[0], [0.0, 0.0, 0.0])
    assert np.allclose(points[1], [0.025, 0.0, 0.0])
    assert np.allclose(points[9], [0.0, 0.025, 0.0])


def test_write_calibration_json_persists_expected_shape(tmp_path: Path) -> None:
    output_path = tmp_path / "camera.json"
    calibration = CameraCalibration(
        camera_matrix=np.array(
            [[900.0, 0.0, 320.0], [0.0, 900.0, 240.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        ),
        distortion_coefficients=np.zeros(5, dtype=np.float64),
        image_width=640,
        image_height=480,
        source_path="memory://generated",
        source_format="json",
    )

    write_calibration_json(
        output_path=output_path,
        calibration=calibration,
        rms_error=0.12,
        board_cols=9,
        board_rows=6,
        square_size_m=0.025,
        image_count=18,
    )

    payload = json.loads(output_path.read_text())

    assert payload["board"]["inner_corners"] == {"cols": 9, "rows": 6}
    assert payload["board"]["square_size_m"] == 0.025
    assert payload["image_count"] == 18
    assert payload["image_width"] == 640
    assert payload["camera_matrix"][0][0] == 900.0


def test_cli_invokes_calibration_pipeline(tmp_path: Path) -> None:
    images_dir = tmp_path / "captures"
    images_dir.mkdir()
    (images_dir / "frame1.jpg").write_bytes(b"fake")
    output_path = tmp_path / "camera.json"

    fake_result = CalibrationResult(
        calibration=CameraCalibration(
            camera_matrix=np.eye(3, dtype=np.float64),
            distortion_coefficients=np.zeros(5, dtype=np.float64),
            image_width=640,
            image_height=480,
            source_path="memory://generated",
            source_format="json",
        ),
        rms_error=0.15,
        image_count=12,
    )

    runner = CliRunner()
    with patch.object(
        camera_calibrate_module,
        "calibrate_from_directory",
        return_value=fake_result,
    ) as mock_calibrate:
        result = runner.invoke(
            cli,
            [
                "--images-dir",
                str(images_dir),
                "--output",
                str(output_path),
            ],
        )

    assert result.exit_code == 0
    mock_calibrate.assert_called_once()
    assert output_path.exists()


def test_cli_capture_mode_collects_images_before_calibration(tmp_path: Path) -> None:
    images_dir = tmp_path / "captures"
    output_path = tmp_path / "camera.json"

    fake_result = CalibrationResult(
        calibration=CameraCalibration(
            camera_matrix=np.eye(3, dtype=np.float64),
            distortion_coefficients=np.zeros(5, dtype=np.float64),
            image_width=640,
            image_height=480,
            source_path="memory://generated",
            source_format="json",
        ),
        rms_error=0.15,
        image_count=12,
    )

    runner = CliRunner()
    with patch.object(
        camera_calibrate_module,
        "capture_chessboard_images",
        return_value=9,
    ) as mock_capture, patch.object(
        camera_calibrate_module,
        "calibrate_from_directory",
        return_value=fake_result,
    ) as mock_calibrate:
        result = runner.invoke(
            cli,
            [
                "--images-dir",
                str(images_dir),
                "--output",
                str(output_path),
                "--capture",
                "--device",
                "3",
            ],
        )

    assert result.exit_code == 0
    mock_capture.assert_called_once_with(images_dir=images_dir, device=3, cols=9, rows=6)
    mock_calibrate.assert_called_once()
    assert output_path.exists()
