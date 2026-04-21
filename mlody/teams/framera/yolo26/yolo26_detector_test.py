"""Tests for the Framera YOLOv26 CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from mlody.teams.framera.yolo26 import yolo26_detector as detector_module
from mlody.teams.framera.yolo26.runtime import default_model_path_for_task

cli = detector_module.cli


def test_cli_invokes_runtime_with_expected_config(tmp_path: Path) -> None:
    model_path = tmp_path / "yolo26n.pt"
    model_path.write_text("weights")
    runner = CliRunner()

    with patch.object(detector_module, "run_camera_session") as mock_run:
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
                "--task",
                "segmentation",
                "--model",
                str(model_path),
                "--conf",
                "0.35",
                "--iou",
                "0.55",
                "--max-det",
                "150",
                "--gpu",
                "--isolate",
                "0",
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
    assert config.task == "segmentation"
    assert config.model_path == model_path
    assert config.conf == 0.35
    assert config.iou == 0.55
    assert config.max_det == 150
    assert config.gpu is True
    assert config.isolate_class == 0
    assert config.emit_json is False
    assert config.gui is True


def test_cli_surfaces_runtime_errors(tmp_path: Path) -> None:
    model_path = tmp_path / "yolo26n.pt"
    model_path.write_text("weights")
    runner = CliRunner()

    with patch.object(detector_module, "run_camera_session", side_effect=RuntimeError("boom")):
        result = runner.invoke(
            cli,
            [
                "--model",
                str(model_path),
            ],
        )

    assert result.exit_code != 0
    assert "Error: boom" in result.output


def test_cli_defaults_device_to_one(tmp_path: Path) -> None:
    model_path = tmp_path / "yolo26x.pt"
    model_path.write_text("weights")
    runner = CliRunner()

    with patch.object(detector_module, "run_camera_session") as mock_run:
        result = runner.invoke(
            cli,
            [
                "--model",
                str(model_path),
            ],
        )

    assert result.exit_code == 0
    config = mock_run.call_args.kwargs["config"]
    assert config.device == 1
    assert config.isolate_class is None


def test_cli_defaults_model_from_task_when_model_not_provided() -> None:
    runner = CliRunner()

    with patch.object(detector_module, "run_camera_session") as mock_run:
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    config = mock_run.call_args.kwargs["config"]
    assert config.task == "detection"
    assert config.model_path == default_model_path_for_task(task="detection")


def test_cli_uses_segmentation_default_model_when_task_is_segmentation() -> None:
    runner = CliRunner()

    with patch.object(detector_module, "run_camera_session") as mock_run:
        result = runner.invoke(cli, ["--task", "segmentation"])

    assert result.exit_code == 0
    config = mock_run.call_args.kwargs["config"]
    assert config.task == "segmentation"
    assert config.model_path == default_model_path_for_task(task="segmentation")


def test_cli_rejects_isolate_for_non_segmentation_task() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--task", "detection", "--isolate", "0"])

    assert result.exit_code != 0
    assert "--isolate is only valid" in result.output
