"""Runtime loop for the framera pose estimator."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .calibration import CameraCalibration, load_calibration
from .geometry import (
    reconstruct_face_landmarks_camera_space,
    reconstruct_hand_landmarks_camera_space,
    reconstruct_pose_landmarks_camera_space,
)
from .mediapipe_adapter import MediaPipeTracker
from .mediapipe_adapter import NormalizedLandmark
from .schema import FramePacket, Landmark3D


@dataclass(frozen=True)
class SessionConfig:
    """Runtime configuration for one camera session."""

    device: int
    width: int
    height: int
    fps: int
    emit_interval_ms: int
    emit_json: bool
    gui: bool
    gpu: bool
    body: bool
    hands: bool
    calibration_path: Path
    holistic_model_path: Path | None = None
    face_model_path: Path | None = None
    pose_model_path: Path | None = None
    hand_model_path: Path | None = None


@dataclass
class EmissionGate:
    """Time-based stdout emission gate."""

    interval_seconds: float
    _last_emit: float | None = None

    def should_emit(self, now: float) -> bool:
        """Return whether a frame should be emitted now."""
        if self._last_emit is None:
            self._last_emit = now
            return True
        if (now - self._last_emit) >= self.interval_seconds:
            self._last_emit = now
            return True
        return False


@dataclass
class FpsCounter:
    """Smoothed GUI FPS estimator."""

    smoothing: float = 0.2
    _last_timestamp: float | None = None
    _smoothed_fps: float = 0.0

    def update(self, now: float) -> float:
        """Update the estimator and return the current smoothed FPS."""
        if self._last_timestamp is None:
            self._last_timestamp = now
            return 0.0
        delta = now - self._last_timestamp
        self._last_timestamp = now
        if delta <= 0:
            return self._smoothed_fps
        instantaneous_fps = 1.0 / delta
        if self._smoothed_fps == 0.0:
            self._smoothed_fps = instantaneous_fps
        else:
            self._smoothed_fps = (
                self.smoothing * instantaneous_fps
                + (1.0 - self.smoothing) * self._smoothed_fps
            )
        return self._smoothed_fps


def choose_status(*, face_count: int, pose_count: int, hand_count: int, degraded: bool) -> str:
    """Select a stream status string."""
    if face_count == 0 and pose_count == 0 and hand_count == 0:
        return "no_detection"
    if degraded:
        return "degraded"
    return "ok"


def build_overlay_lines(*, status: str, fps: float) -> tuple[str, ...]:
    """Build the overlay text lines shown in the GUI."""
    return (f"Status: {status}", f"FPS: {fps:.1f}")


def run_camera_session(*, config: SessionConfig) -> None:
    """Run the capture, inference, emit, and optional GUI loop."""
    cv2 = _load_cv2()
    calibration = load_calibration(config.calibration_path)
    capture = _open_capture(config=config, cv2=cv2)
    gate = EmissionGate(interval_seconds=config.emit_interval_ms / 1000.0)
    fps_counter = FpsCounter()

    try:
        with MediaPipeTracker(
            holistic_model_path=(
                str(config.holistic_model_path)
                if config.holistic_model_path is not None
                else None
            ),
            face_model_path=(
                str(config.face_model_path)
                if config.face_model_path is not None
                else None
            ),
            pose_model_path=(
                str(config.pose_model_path)
                if config.pose_model_path is not None
                else None
            ),
            hand_model_path=(
                str(config.hand_model_path)
                if config.hand_model_path is not None
                else None
            ),
            body_enabled=config.body,
            hands_enabled=config.hands,
            use_gpu=config.gpu,
        ) as tracker:
            while True:
                ok, frame = capture.read()
                if not ok:
                    time.sleep(0.01)
                    continue

                frame_height, frame_width = frame.shape[:2]
                calibration.validate_capture_size(width=frame_width, height=frame_height)

                result = tracker.process(frame)
                pose_landmarks, pose_degraded, pose_warnings = (
                    reconstruct_pose_landmarks_camera_space(
                        calibration=calibration,
                        pose_landmarks=result.pose_landmarks,
                        pose_world_landmarks=result.pose_world_landmarks,
                    )
                    if config.body
                    else ((), False, ())
                )
                face_landmarks, face_degraded, face_warnings = (
                    reconstruct_face_landmarks_camera_space(
                        calibration=calibration,
                        face_landmarks=result.face_landmarks,
                    )
                )
                left_hand_landmarks, left_hand_degraded, left_hand_warnings = (
                    _reconstruct_hand_if_available(
                        calibration=calibration,
                        hand_landmarks=result.left_hand_landmarks,
                        hand_world_landmarks=result.left_hand_world_landmarks,
                        hand_label="left",
                    )
                    if config.hands
                    else ((), False, ())
                )
                right_hand_landmarks, right_hand_degraded, right_hand_warnings = (
                    _reconstruct_hand_if_available(
                        calibration=calibration,
                        hand_landmarks=result.right_hand_landmarks,
                        hand_world_landmarks=result.right_hand_world_landmarks,
                        hand_label="right",
                    )
                    if config.hands
                    else ((), False, ())
                )

                warnings = tuple(
                    sorted(
                        {
                            *pose_warnings,
                            *face_warnings,
                            *left_hand_warnings,
                            *right_hand_warnings,
                        }
                    )
                )
                degraded = (
                    pose_degraded
                    or face_degraded
                    or left_hand_degraded
                    or right_hand_degraded
                )
                status = choose_status(
                    face_count=len(face_landmarks),
                    pose_count=len(pose_landmarks),
                    hand_count=len(left_hand_landmarks) + len(right_hand_landmarks),
                    degraded=degraded,
                )

                if config.emit_json and gate.should_emit(time.monotonic()):
                    packet = FramePacket(
                        timestamp_ms=int(time.time() * 1000),
                        frame_width=frame_width,
                        frame_height=frame_height,
                        status=status,
                        degraded=degraded,
                        metric_3d=not degraded,
                        warnings=warnings,
                        calibration=calibration.metadata(),
                        face=face_landmarks,
                        pose=pose_landmarks,
                        left_hand=left_hand_landmarks,
                        right_hand=right_hand_landmarks,
                    )
                    print(packet.to_json_line(), flush=True)

                if config.gui:
                    fps = fps_counter.update(time.monotonic())
                    annotated = tracker.draw_overlay(
                        frame,
                        result,
                        overlay_lines=build_overlay_lines(status=status, fps=fps),
                    )
                    cv2.imshow("framera pose estimator", annotated)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q")):
                        break
    finally:
        capture.release()
        if config.gui:
            cv2.destroyAllWindows()


def _open_capture(*, config: SessionConfig, cv2: Any) -> Any:
    capture = cv2.VideoCapture(config.device)
    if not capture.isOpened():
        raise RuntimeError(f"failed to open camera device {config.device}")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(config.width))
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(config.height))
    capture.set(cv2.CAP_PROP_FPS, float(config.fps))
    return capture


def _load_cv2() -> Any:
    import cv2

    return cv2


def _reconstruct_hand_if_available(
    *,
    calibration: CameraCalibration,
    hand_landmarks: tuple[NormalizedLandmark, ...],
    hand_world_landmarks: tuple[NormalizedLandmark, ...],
    hand_label: str,
) -> tuple[tuple[Landmark3D, ...], bool, tuple[str, ...]]:
    if not hand_landmarks and not hand_world_landmarks:
        return (), False, ()
    return reconstruct_hand_landmarks_camera_space(
        calibration=calibration,
        hand_landmarks=hand_landmarks,
        hand_world_landmarks=hand_world_landmarks,
        hand_label=hand_label,
    )
