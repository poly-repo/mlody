"""Tests for the framera CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from mlody.teams.framera.pose_estimation import pose_estimator as pose_estimator_module

cli = pose_estimator_module.cli


def test_cli_invokes_runtime_with_expected_config(tmp_path: Path) -> None:
    calibration_path = tmp_path / "camera.json"
    calibration_path.write_text("{}")
    face_model_path = tmp_path / "face.task"
    pose_model_path = tmp_path / "pose.task"
    hand_model_path = tmp_path / "hand.task"
    face_model_path.write_text("face")
    pose_model_path.write_text("pose")
    hand_model_path.write_text("hand")

    runner = CliRunner()

    with patch.object(pose_estimator_module, "run_camera_session") as mock_run:
        result = runner.invoke(
            cli,
            [
                "--device",
                "2",
                "--width",
                "800",
                "--height",
                "600",
                "--fps",
                "24",
                "--emit-interval-ms",
                "200",
                "--calibration",
                str(calibration_path),
                "--face-model",
                str(face_model_path),
                "--body",
                "--pose-model",
                str(pose_model_path),
                "--hands",
                "--hand-model",
                str(hand_model_path),
                "--gpu",
                "--no-json",
                "--gui",
            ],
        )

    assert result.exit_code == 0
    config = mock_run.call_args.kwargs["config"]
    assert config.device == 2
    assert config.width == 800
    assert config.height == 600
    assert config.fps == 24
    assert config.emit_interval_ms == 200
    assert config.gui is True
    assert config.gpu is True
    assert config.body is True
    assert config.hands is True
    assert config.emit_json is False
    assert config.calibration_path == calibration_path
    assert config.holistic_model_path is None
    assert config.face_model_path == face_model_path
    assert config.pose_model_path == pose_model_path
    assert config.hand_model_path == hand_model_path


def test_cli_allows_unused_optional_models_without_body_or_hands(tmp_path: Path) -> None:
    calibration_path = tmp_path / "camera.json"
    calibration_path.write_text("{}")
    face_model_path = tmp_path / "face.task"
    pose_model_path = tmp_path / "pose.task"
    hand_model_path = tmp_path / "hand.task"
    face_model_path.write_text("face")
    pose_model_path.write_text("pose")
    hand_model_path.write_text("hand")

    runner = CliRunner()

    with patch.object(pose_estimator_module, "run_camera_session") as mock_run:
        result = runner.invoke(
            cli,
            [
                "--calibration",
                str(calibration_path),
                "--face-model",
                str(face_model_path),
                "--pose-model",
                str(pose_model_path),
                "--hand-model",
                str(hand_model_path),
            ],
        )

    assert result.exit_code == 0
    config = mock_run.call_args.kwargs["config"]
    assert config.body is False
    assert config.hands is False
    assert config.face_model_path == face_model_path
    assert config.pose_model_path == pose_model_path
    assert config.hand_model_path == hand_model_path
