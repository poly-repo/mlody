"""Calibration loading helpers for the framera pose estimator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt


FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class CameraCalibration:
    """Typed OpenCV-style camera calibration."""

    camera_matrix: FloatArray
    distortion_coefficients: FloatArray
    image_width: int
    image_height: int
    source_path: str
    source_format: str

    def validate_capture_size(self, *, width: int, height: int) -> None:
        """Ensure the requested or observed capture mode matches calibration."""
        if self.image_width != width or self.image_height != height:
            raise ValueError(
                "calibration capture size "
                f"{self.image_width}x{self.image_height} does not match "
                f"requested capture size {width}x{height}"
            )

    def metadata(self) -> dict[str, object]:
        """Return JSON-safe metadata for the output stream."""
        return {
            "source_path": self.source_path,
            "source_format": self.source_format,
            "image_width": self.image_width,
            "image_height": self.image_height,
        }


def load_calibration(path: Path) -> CameraCalibration:
    """Load a calibration artifact from JSON or OpenCV YAML."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json_calibration(path)
    if suffix in {".yaml", ".yml"}:
        return _load_yaml_calibration(path)
    raise ValueError(f"unsupported calibration format: {path}")


def _load_json_calibration(path: Path) -> CameraCalibration:
    payload = json.loads(path.read_text())
    camera_matrix = _coerce_matrix(payload.get("camera_matrix"), shape=(3, 3), name="camera_matrix")
    distortion = _coerce_vector(
        payload.get("distortion_coefficients"),
        name="distortion_coefficients",
    )
    image_width = _coerce_int(payload.get("image_width"), name="image_width")
    image_height = _coerce_int(payload.get("image_height"), name="image_height")
    return CameraCalibration(
        camera_matrix=camera_matrix,
        distortion_coefficients=distortion,
        image_width=image_width,
        image_height=image_height,
        source_path=str(path),
        source_format="json",
    )


def _load_yaml_calibration(path: Path) -> CameraCalibration:
    cv2 = _load_cv2()
    storage = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
    if not storage.isOpened():
        raise ValueError(f"failed to open calibration file: {path}")
    try:
        camera_matrix = _coerce_matrix(
            storage.getNode("camera_matrix").mat(),
            shape=(3, 3),
            name="camera_matrix",
        )
        distortion = _coerce_vector(
            storage.getNode("distortion_coefficients").mat(),
            name="distortion_coefficients",
        )
        image_width = _coerce_int(storage.getNode("image_width").real(), name="image_width")
        image_height = _coerce_int(storage.getNode("image_height").real(), name="image_height")
    finally:
        storage.release()
    return CameraCalibration(
        camera_matrix=camera_matrix,
        distortion_coefficients=distortion,
        image_width=image_width,
        image_height=image_height,
        source_path=str(path),
        source_format="yaml",
    )


def _coerce_matrix(value: Any, *, shape: tuple[int, int], name: str) -> FloatArray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != shape:
        raise ValueError(f"{name} must have shape {shape}, got {matrix.shape}")
    return matrix


def _coerce_vector(value: Any, *, name: str) -> FloatArray:
    vector = np.asarray(value, dtype=np.float64).reshape(-1)
    if vector.size < 4:
        raise ValueError(f"{name} must contain at least four coefficients")
    return vector


def _coerce_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _load_cv2() -> Any:
    import cv2

    return cv2
