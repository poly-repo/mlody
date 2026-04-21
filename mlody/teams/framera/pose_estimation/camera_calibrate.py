"""Offline camera calibration utility for framera."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import numpy as np
import numpy.typing as npt

from .calibration import CameraCalibration


FloatArray = npt.NDArray[np.float32]


@dataclass(frozen=True)
class CalibrationResult:
    """One successful camera-calibration run."""

    calibration: CameraCalibration
    rms_error: float
    image_count: int


@click.command()
@click.option(
    "--images-dir",
    type=click.Path(path_type=Path, file_okay=False, readable=True),
    required=True,
    help="Directory containing chessboard photos or where --capture will save them.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False, writable=True),
    required=True,
    help="Where to write the generated calibration JSON.",
)
@click.option("--cols", type=int, default=9, show_default=True, help="Inner-corner columns.")
@click.option("--rows", type=int, default=6, show_default=True, help="Inner-corner rows.")
@click.option(
    "--capture",
    is_flag=True,
    default=False,
    help="Capture calibration images from a live webcam before calibrating.",
)
@click.option("--device", type=int, default=0, show_default=True, help="Camera device index for --capture mode.")
@click.option(
    "--square-size-mm",
    type=float,
    default=25.0,
    show_default=True,
    help="Physical square size on the printed board in millimeters.",
)
def cli(
    *,
    images_dir: Path,
    output: Path,
    cols: int,
    rows: int,
    capture: bool,
    device: int,
    square_size_mm: float,
) -> None:
    """Calibrate a camera from chessboard images and emit OpenCV-style JSON."""
    try:
        if capture:
            capture_chessboard_images(
                images_dir=images_dir,
                device=device,
                cols=cols,
                rows=rows,
            )
        result = calibrate_from_directory(
            images_dir=images_dir,
            cols=cols,
            rows=rows,
            square_size_m=square_size_mm / 1000.0,
        )
        write_calibration_json(
            output_path=output,
            calibration=result.calibration,
            rms_error=result.rms_error,
            board_cols=cols,
            board_rows=rows,
            square_size_m=square_size_mm / 1000.0,
            image_count=result.image_count,
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def build_object_points(*, cols: int, rows: int, square_size_m: float) -> FloatArray:
    """Build the chessboard object points grid in meters."""
    if cols <= 0 or rows <= 0:
        raise ValueError("board dimensions must be positive")
    if square_size_m <= 0:
        raise ValueError("square size must be positive")
    grid = np.zeros((rows * cols, 3), dtype=np.float32)
    x_coords, y_coords = np.meshgrid(np.arange(cols), np.arange(rows), indexing="xy")
    grid[:, 0] = x_coords.reshape(-1) * square_size_m
    grid[:, 1] = y_coords.reshape(-1) * square_size_m
    return grid


def calibrate_from_directory(
    *,
    images_dir: Path,
    cols: int,
    rows: int,
    square_size_m: float,
) -> CalibrationResult:
    """Calibrate from a directory of chessboard images."""
    cv2 = _load_cv2()
    if not images_dir.exists():
        raise ValueError(f"image directory does not exist: {images_dir}")
    image_paths = sorted(path for path in images_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not image_paths:
        raise ValueError(f"no calibration images found in {images_dir}")

    board_size = (cols, rows)
    object_template = build_object_points(cols=cols, rows=rows, square_size_m=square_size_m)
    object_points: list[FloatArray] = []
    image_points: list[FloatArray] = []
    image_size: tuple[int, int] | None = None

    for image_path in image_paths:
        image = cv2.imread(str(image_path))
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        found, corners = cv2.findChessboardCorners(gray, board_size)
        if not found:
            continue
        refined = cv2.cornerSubPix(
            gray,
            corners,
            winSize=(11, 11),
            zeroZone=(-1, -1),
            criteria=(
                cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                30,
                0.001,
            ),
        )
        object_points.append(object_template.copy())
        image_points.append(refined.reshape(-1, 2).astype(np.float32))
        image_size = (gray.shape[1], gray.shape[0])

    if image_size is None or len(image_points) < 8:
        raise RuntimeError("need at least 8 images with detected chessboard corners")

    rms_error, camera_matrix, distortion, _, _ = cv2.calibrateCamera(
        object_points,
        image_points,
        image_size,
        None,
        None,
    )
    calibration = CameraCalibration(
        camera_matrix=np.asarray(camera_matrix, dtype=np.float64),
        distortion_coefficients=np.asarray(distortion, dtype=np.float64).reshape(-1),
        image_width=image_size[0],
        image_height=image_size[1],
        source_path=str(images_dir),
        source_format="json",
    )
    return CalibrationResult(
        calibration=calibration,
        rms_error=float(rms_error),
        image_count=len(image_points),
    )


def capture_chessboard_images(
    *,
    images_dir: Path,
    device: int,
    cols: int,
    rows: int,
) -> int:
    """Capture chessboard photos from a webcam into the target directory.

    Press `space` to save the current frame when a chessboard is visible.
    Press `q` or `esc` to finish capture.
    """
    cv2 = _load_cv2()
    images_dir.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(device)
    if not capture.isOpened():
        raise RuntimeError(f"failed to open camera device {device}")

    board_size = (cols, rows)
    saved_count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found, corners = cv2.findChessboardCorners(gray, board_size)
            preview = frame.copy()
            cv2.drawChessboardCorners(preview, board_size, corners, found)
            cv2.putText(
                preview,
                f"Saved: {saved_count}  Space=save  Q/Esc=finish",
                (20, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0) if found else (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow("framera calibration capture", preview)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if key == 32 and found:
                output_path = images_dir / f"capture-{saved_count + 1:03d}.jpg"
                cv2.imwrite(str(output_path), frame)
                saved_count += 1
    finally:
        capture.release()
        cv2.destroyAllWindows()
    return saved_count


def write_calibration_json(
    *,
    output_path: Path,
    calibration: CameraCalibration,
    rms_error: float,
    board_cols: int,
    board_rows: int,
    square_size_m: float,
    image_count: int,
) -> None:
    """Write a calibration result as framera/OpenCV-compatible JSON."""
    payload = {
        "camera_matrix": calibration.camera_matrix.tolist(),
        "distortion_coefficients": calibration.distortion_coefficients.tolist(),
        "image_width": calibration.image_width,
        "image_height": calibration.image_height,
        "rms_error": rms_error,
        "image_count": image_count,
        "board": {
            "inner_corners": {"cols": board_cols, "rows": board_rows},
            "square_size_m": square_size_m,
        },
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n")


def _load_cv2() -> Any:
    import cv2

    return cv2


def main() -> None:
    """Execute the calibration CLI."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
