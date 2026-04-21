"""CLI entrypoint for the framera pose estimator."""

from __future__ import annotations

from pathlib import Path

import click

from .runtime import SessionConfig, run_camera_session


@click.command()
@click.option("--device", type=int, default=0, show_default=True, help="Camera device index.")
@click.option("--width", type=int, default=640, show_default=True, help="Requested capture width.")
@click.option("--height", type=int, default=480, show_default=True, help="Requested capture height.")
@click.option("--fps", type=int, default=30, show_default=True, help="Requested capture FPS.")
@click.option(
    "--emit-interval-ms",
    type=int,
    default=100,
    show_default=True,
    help="How often to emit one JSON frame on stdout.",
)
@click.option(
    "--calibration",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    required=True,
    help="Path to an OpenCV JSON or YAML calibration file.",
)
@click.option(
    "--holistic-model",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    default=None,
    help="Optional path to a MediaPipe holistic_landmarker.task model for Tasks-only installs.",
)
@click.option(
    "--face-model",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    default=None,
    help="Optional path to a MediaPipe face_landmarker.task model.",
)
@click.option(
    "--body",
    is_flag=True,
    default=False,
    help="Enable body pose estimation and overlay output.",
)
@click.option(
    "--pose-model",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    default=None,
    help="Optional path to a MediaPipe pose_landmarker.task model.",
)
@click.option(
    "--hands",
    is_flag=True,
    default=False,
    help="Enable left/right hand landmark estimation and overlay output.",
)
@click.option(
    "--hand-model",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    default=None,
    help="Optional path to a MediaPipe hand_landmarker.task model for the split Tasks backend.",
)
@click.option(
    "--gpu",
    is_flag=True,
    default=False,
    help="Request MediaPipe GPU delegate when using the Tasks backend.",
)
@click.option(
    "--no-json",
    is_flag=True,
    default=False,
    help="Disable JSONL frame emission to stdout.",
)
@click.option("--gui", is_flag=True, default=False, help="Show a live overlay window.")
def cli(
    *,
    device: int,
    width: int,
    height: int,
    fps: int,
    emit_interval_ms: int,
    calibration: Path,
    holistic_model: Path | None,
    face_model: Path | None,
    body: bool,
    pose_model: Path | None,
    hands: bool,
    hand_model: Path | None,
    gpu: bool,
    no_json: bool,
    gui: bool,
) -> None:
    """Run realtime face landmarking and pose estimation from a webcam."""
    config = SessionConfig(
        device=device,
        width=width,
        height=height,
        fps=fps,
        emit_interval_ms=emit_interval_ms,
        emit_json=not no_json,
        gui=gui,
        gpu=gpu,
        body=body,
        hands=hands,
        calibration_path=calibration,
        holistic_model_path=holistic_model,
        face_model_path=face_model,
        pose_model_path=pose_model,
        hand_model_path=hand_model,
    )
    try:
        run_camera_session(config=config)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def main() -> None:
    """Execute the Click CLI."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
