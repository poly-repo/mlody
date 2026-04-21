"""CLI entrypoint for Framera YOLOv26 webcam inference tasks."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from mlody.teams.framera.yolo26.runtime import (
    SUPPORTED_TASKS,
    SessionConfig,
    TaskName,
    default_model_path_for_task,
    run_camera_session,
)


@click.command()
@click.option("--device", type=int, default=1, show_default=True, help="Camera device index.")
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
    "--task",
    type=click.Choice(SUPPORTED_TASKS, case_sensitive=False),
    default="detection",
    show_default=True,
    help="YOLO task variant to run.",
)
@click.option(
    "--model",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    default=None,
    show_default=False,
    help=(
        "Path to a YOLOv26 PyTorch model checkpoint (.pt). "
        "Defaults to the task-specific yolo26x model."
    ),
)
@click.option(
    "--conf",
    type=float,
    default=0.25,
    show_default=True,
    help="Detection confidence threshold.",
)
@click.option(
    "--iou",
    type=float,
    default=0.7,
    show_default=True,
    help="NMS IoU threshold.",
)
@click.option(
    "--max-det",
    type=int,
    default=300,
    show_default=True,
    help="Maximum detections per frame.",
)
@click.option(
    "--gpu",
    is_flag=True,
    default=False,
    help="Request CUDA inference when available.",
)
@click.option(
    "--isolate",
    type=int,
    default=None,
    help=(
        "Segmentation-only: class id to isolate. "
        "Non-target pixels are replaced with green in GUI mode."
    ),
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
    task: str,
    model: Path | None,
    conf: float,
    iou: float,
    max_det: int,
    gpu: bool,
    isolate: int | None,
    no_json: bool,
    gui: bool,
) -> None:
    """Run realtime YOLOv26 webcam inference for the selected task."""
    selected_task_raw = task.lower()
    if selected_task_raw not in SUPPORTED_TASKS:
        raise click.ClickException(f"unsupported task: {selected_task_raw}")
    selected_task = cast(TaskName, selected_task_raw)
    model_path = (
        model
        if model is not None
        else default_model_path_for_task(task=selected_task)
    )
    if selected_task == "segmentation" and "-seg" not in model_path.stem:
        click.echo(
            (
                "Warning: --task segmentation typically expects a *-seg.pt model. "
                f"Current model is '{model_path.name}'."
            ),
            err=True,
        )
    if isolate is not None and selected_task != "segmentation":
        raise click.ClickException("--isolate is only valid when --task segmentation is selected.")
    config = SessionConfig(
        task=selected_task,
        device=device,
        width=width,
        height=height,
        fps=fps,
        emit_interval_ms=emit_interval_ms,
        emit_json=not no_json,
        gui=gui,
        gpu=gpu,
        model_path=model_path,
        conf=conf,
        iou=iou,
        max_det=max_det,
        isolate_class=isolate,
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
