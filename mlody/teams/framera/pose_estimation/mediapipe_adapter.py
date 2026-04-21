"""MediaPipe integration boundary for framera."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy.typing as npt


@dataclass(frozen=True)
class NormalizedLandmark:
    """One MediaPipe-style landmark."""

    index: int
    x: float
    y: float
    z: float
    visibility: float | None
    presence: float | None = None


@dataclass(frozen=True)
class TrackingResult:
    """Normalized landmark sets returned by the tracker."""

    face_landmarks: tuple[NormalizedLandmark, ...]
    pose_landmarks: tuple[NormalizedLandmark, ...]
    pose_world_landmarks: tuple[NormalizedLandmark, ...]
    left_hand_landmarks: tuple[NormalizedLandmark, ...] = ()
    left_hand_world_landmarks: tuple[NormalizedLandmark, ...] = ()
    right_hand_landmarks: tuple[NormalizedLandmark, ...] = ()
    right_hand_world_landmarks: tuple[NormalizedLandmark, ...] = ()
    raw_face_landmarks: object | None = None
    raw_pose_landmarks: object | None = None
    raw_left_hand_landmarks: object | None = None
    raw_right_hand_landmarks: object | None = None


class MediaPipeTracker:
    """Thin wrapper around MediaPipe Holistic."""

    def __init__(
        self,
        *,
        holistic_model_path: str | None = None,
        face_model_path: str | None = None,
        pose_model_path: str | None = None,
        hand_model_path: str | None = None,
        body_enabled: bool = False,
        hands_enabled: bool = False,
        use_gpu: bool = False,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ) -> None:
        self._holistic_model_path = holistic_model_path
        self._face_model_path = face_model_path
        self._pose_model_path = pose_model_path
        self._hand_model_path = hand_model_path
        self._body_enabled = body_enabled
        self._hands_enabled = hands_enabled
        self._use_gpu = use_gpu
        self._min_detection_confidence = min_detection_confidence
        self._min_tracking_confidence = min_tracking_confidence
        self._solutions: Any | None = None
        self._tasks_mp: Any | None = None
        self._backend: str | None = None
        self._holistic: Any | None = None
        self._face_landmarker: Any | None = None
        self._pose_landmarker: Any | None = None
        self._hand_landmarker: Any | None = None

    def __enter__(self) -> "MediaPipeTracker":
        self._solutions = _try_load_mediapipe_solutions()
        if self._solutions is not None:
            self._backend = "solutions"
            self._holistic = self._solutions.holistic.Holistic(
                static_image_mode=False,
                model_complexity=1,
                refine_face_landmarks=True,
                min_detection_confidence=self._min_detection_confidence,
                min_tracking_confidence=self._min_tracking_confidence,
            )
            return self

        if self._face_model_path is not None:
            if self._body_enabled and self._pose_model_path is None:
                raise RuntimeError(
                    "The Tasks backend requires --pose-model when --body is enabled."
                )
            if self._hands_enabled and self._hand_model_path is None:
                raise RuntimeError(
                    "The Tasks backend requires --hand-model when --hands is enabled."
                )
            self._backend = "tasks_components"
            self._tasks_mp = _import_mediapipe()
            self._face_landmarker = _create_task_face_landmarker(
                model_path=self._face_model_path,
                use_gpu=self._use_gpu,
            )
            if self._body_enabled and self._pose_model_path is not None:
                self._pose_landmarker = _create_task_pose_landmarker(
                    model_path=self._pose_model_path,
                    use_gpu=self._use_gpu,
                )
            if self._hands_enabled and self._hand_model_path is not None:
                self._hand_landmarker = _create_task_hand_landmarker(
                    model_path=self._hand_model_path,
                    use_gpu=self._use_gpu,
                )
            return self

        if self._holistic_model_path is None:
            raise RuntimeError(
                "The installed mediapipe wheel exposes only the Tasks API. "
                "Pass --face-model, plus --body with --pose-model and/or --hands "
                "with --hand-model as needed, or "
                "--holistic-model /path/to/holistic_landmarker.task."
            )

        self._backend = "tasks"
        self._tasks_mp = _import_mediapipe()
        self._holistic = _create_task_holistic_landmarker(
            model_path=self._holistic_model_path,
            use_gpu=self._use_gpu,
        )
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._holistic is not None and hasattr(self._holistic, "close"):
            self._holistic.close()
        if self._face_landmarker is not None and hasattr(self._face_landmarker, "close"):
            self._face_landmarker.close()
        if self._pose_landmarker is not None and hasattr(self._pose_landmarker, "close"):
            self._pose_landmarker.close()
        if self._hand_landmarker is not None and hasattr(self._hand_landmarker, "close"):
            self._hand_landmarker.close()
        self._holistic = None
        self._face_landmarker = None
        self._pose_landmarker = None
        self._hand_landmarker = None
        self._solutions = None
        self._tasks_mp = None
        self._backend = None

    def process(self, frame_bgr: npt.NDArray[object]) -> TrackingResult:
        """Run MediaPipe on one BGR frame."""
        if self._backend == "tasks_components":
            if self._face_landmarker is None:
                raise RuntimeError("MediaPipeTracker must be entered before use")
        elif self._holistic is None:
            raise RuntimeError("MediaPipeTracker must be entered before use")
        cv2 = _load_cv2()
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        if self._backend == "solutions":
            results = self._holistic.process(frame_rgb)
            return TrackingResult(
                face_landmarks=_convert_landmarks(results.face_landmarks),
                pose_landmarks=(
                    _convert_landmarks(results.pose_landmarks) if self._body_enabled else ()
                ),
                pose_world_landmarks=(
                    _convert_landmarks(results.pose_world_landmarks)
                    if self._body_enabled
                    else ()
                ),
                left_hand_landmarks=_convert_landmarks(
                    getattr(results, "left_hand_landmarks", None)
                )
                if self._hands_enabled
                else (),
                left_hand_world_landmarks=_convert_landmarks(
                    getattr(results, "left_hand_world_landmarks", None)
                )
                if self._hands_enabled
                else (),
                right_hand_landmarks=_convert_landmarks(
                    getattr(results, "right_hand_landmarks", None)
                )
                if self._hands_enabled
                else (),
                right_hand_world_landmarks=_convert_landmarks(
                    getattr(results, "right_hand_world_landmarks", None)
                )
                if self._hands_enabled
                else (),
                raw_face_landmarks=results.face_landmarks,
                raw_pose_landmarks=results.pose_landmarks if self._body_enabled else None,
                raw_left_hand_landmarks=(
                    getattr(results, "left_hand_landmarks", None) if self._hands_enabled else None
                ),
                raw_right_hand_landmarks=(
                    getattr(results, "right_hand_landmarks", None)
                    if self._hands_enabled
                    else None
                ),
            )

        if self._backend == "tasks_components":
            if self._tasks_mp is None or self._face_landmarker is None:
                raise RuntimeError("Tasks MediaPipe backend was not initialized")
            mp_image = self._tasks_mp.Image(
                image_format=self._tasks_mp.ImageFormat.SRGB,
                data=frame_rgb,
            )
            face_result = self._face_landmarker.detect(mp_image)
            face_landmarks = face_result.face_landmarks[0] if face_result.face_landmarks else []
            pose_landmarks: object = []
            pose_world_landmarks: object = []
            raw_pose_landmarks: object | None = None
            if self._body_enabled and self._pose_landmarker is not None:
                pose_result = self._pose_landmarker.detect(mp_image)
                pose_landmarks = (
                    pose_result.pose_landmarks[0] if pose_result.pose_landmarks else []
                )
                pose_world_landmarks = (
                    pose_result.pose_world_landmarks[0]
                    if pose_result.pose_world_landmarks
                    else []
                )
                raw_pose_landmarks = pose_landmarks
            left_hand_landmarks = ()
            left_hand_world_landmarks = ()
            right_hand_landmarks = ()
            right_hand_world_landmarks = ()
            raw_left_hand_landmarks = None
            raw_right_hand_landmarks = None
            if self._hands_enabled and self._hand_landmarker is not None:
                hand_result = self._hand_landmarker.detect(mp_image)
                split_hands = _split_hand_result(hand_result)
                left_hand_landmarks = _convert_landmarks(split_hands["left"]["landmarks"])
                left_hand_world_landmarks = _convert_landmarks(
                    split_hands["left"]["world_landmarks"]
                )
                right_hand_landmarks = _convert_landmarks(split_hands["right"]["landmarks"])
                right_hand_world_landmarks = _convert_landmarks(
                    split_hands["right"]["world_landmarks"]
                )
                raw_left_hand_landmarks = split_hands["left"]["landmarks"]
                raw_right_hand_landmarks = split_hands["right"]["landmarks"]
            return TrackingResult(
                face_landmarks=_convert_landmarks(face_landmarks),
                pose_landmarks=_convert_landmarks(pose_landmarks),
                pose_world_landmarks=_convert_landmarks(pose_world_landmarks),
                left_hand_landmarks=left_hand_landmarks,
                left_hand_world_landmarks=left_hand_world_landmarks,
                right_hand_landmarks=right_hand_landmarks,
                right_hand_world_landmarks=right_hand_world_landmarks,
                raw_face_landmarks=face_landmarks,
                raw_pose_landmarks=raw_pose_landmarks,
                raw_left_hand_landmarks=raw_left_hand_landmarks,
                raw_right_hand_landmarks=raw_right_hand_landmarks,
            )

        if self._backend == "tasks":
            if self._tasks_mp is None:
                raise RuntimeError("Tasks MediaPipe module was not initialized")
            mp_image = self._tasks_mp.Image(
                image_format=self._tasks_mp.ImageFormat.SRGB,
                data=frame_rgb,
            )
            result = self._holistic.detect(mp_image)
            return TrackingResult(
                face_landmarks=_convert_landmarks(result.face_landmarks),
                pose_landmarks=(
                    _convert_landmarks(result.pose_landmarks) if self._body_enabled else ()
                ),
                pose_world_landmarks=(
                    _convert_landmarks(result.pose_world_landmarks)
                    if self._body_enabled
                    else ()
                ),
                left_hand_landmarks=_convert_landmarks(
                    getattr(result, "left_hand_landmarks", None)
                )
                if self._hands_enabled
                else (),
                left_hand_world_landmarks=_convert_landmarks(
                    getattr(result, "left_hand_world_landmarks", None)
                )
                if self._hands_enabled
                else (),
                right_hand_landmarks=_convert_landmarks(
                    getattr(result, "right_hand_landmarks", None)
                )
                if self._hands_enabled
                else (),
                right_hand_world_landmarks=_convert_landmarks(
                    getattr(result, "right_hand_world_landmarks", None)
                )
                if self._hands_enabled
                else (),
                raw_face_landmarks=result.face_landmarks,
                raw_pose_landmarks=result.pose_landmarks if self._body_enabled else None,
                raw_left_hand_landmarks=(
                    getattr(result, "left_hand_landmarks", None) if self._hands_enabled else None
                ),
                raw_right_hand_landmarks=(
                    getattr(result, "right_hand_landmarks", None)
                    if self._hands_enabled
                    else None
                ),
            )

        raise RuntimeError("MediaPipeTracker backend was not initialized")

    def draw_overlay(
        self,
        frame_bgr: npt.NDArray[object],
        result: TrackingResult,
        *,
        overlay_lines: tuple[str, ...],
    ) -> npt.NDArray[object]:
        """Draw landmarks and status text on a BGR frame."""
        if self._backend == "tasks_components":
            if self._face_landmarker is None:
                raise RuntimeError("MediaPipeTracker must be entered before drawing")
        elif self._holistic is None:
            raise RuntimeError("MediaPipeTracker must be entered before drawing")

        annotated = frame_bgr.copy()
        if self._backend == "solutions" and self._solutions is not None:
            cv2 = _load_cv2()
            drawing = self._solutions.drawing_utils
            styles = self._solutions.drawing_styles
            holistic = self._solutions.holistic
            if result.raw_face_landmarks is not None:
                drawing.draw_landmarks(
                    annotated,
                    result.raw_face_landmarks,
                    holistic.FACEMESH_CONTOURS,
                    landmark_drawing_spec=None,
                    connection_drawing_spec=styles.get_default_face_mesh_contours_style(),
                )
            if result.raw_pose_landmarks is not None:
                drawing.draw_landmarks(
                    annotated,
                    result.raw_pose_landmarks,
                    holistic.POSE_CONNECTIONS,
                    landmark_drawing_spec=styles.get_default_pose_landmarks_style(),
                )
            left_hand_connections = getattr(holistic, "HAND_CONNECTIONS", ())
            if result.raw_left_hand_landmarks is not None:
                drawing.draw_landmarks(
                    annotated,
                    result.raw_left_hand_landmarks,
                    left_hand_connections,
                )
            if result.raw_right_hand_landmarks is not None:
                drawing.draw_landmarks(
                    annotated,
                    result.raw_right_hand_landmarks,
                    left_hand_connections,
                )
            _draw_overlay_lines(annotated, overlay_lines=overlay_lines)
            return annotated

        return _draw_tasks_overlay(annotated, result=result, overlay_lines=overlay_lines)


def _convert_landmarks(landmark_list: object | None) -> tuple[NormalizedLandmark, ...]:
    if landmark_list is None:
        return ()
    if hasattr(landmark_list, "landmark"):
        iterable = landmark_list.landmark
    else:
        iterable = landmark_list
    converted: list[NormalizedLandmark] = []
    for index, landmark in enumerate(iterable):
        converted.append(
            NormalizedLandmark(
                index=int(getattr(landmark, "index", index)),
                x=float(landmark.x),
                y=float(landmark.y),
                z=float(landmark.z),
                visibility=_maybe_float(getattr(landmark, "visibility", None)),
                presence=_maybe_float(getattr(landmark, "presence", None)),
            )
        )
    return tuple(converted)


def _maybe_float(value: object | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _load_cv2() -> Any:
    import cv2

    return cv2


def _try_load_mediapipe_solutions() -> Any | None:
    mp = _import_mediapipe()
    solutions = getattr(mp, "solutions", None)
    if solutions is not None:
        return solutions
    return None


def _import_mediapipe() -> Any:
    import mediapipe as mp

    return mp


def _create_task_holistic_landmarker(*, model_path: str, use_gpu: bool) -> Any:
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.vision.holistic_landmarker import (
        HolisticLandmarker,
        HolisticLandmarkerOptions,
    )

    base_options = BaseOptions(model_asset_path=model_path)
    if use_gpu:
        base_options.delegate = BaseOptions.Delegate.GPU
    options = HolisticLandmarkerOptions(base_options=base_options)
    try:
        return HolisticLandmarker.create_from_options(options)
    except Exception as exc:
        if use_gpu:
            raise RuntimeError(
                "Failed to initialize the MediaPipe GPU delegate. "
                "Verify Ubuntu GPU delegate support, drivers, and EGL/OpenGL "
                "availability, or rerun without --gpu."
            ) from exc
        raise


def _create_task_face_landmarker(*, model_path: str, use_gpu: bool) -> Any:
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.vision.face_landmarker import (
        FaceLandmarker,
        FaceLandmarkerOptions,
    )

    base_options = BaseOptions(model_asset_path=model_path)
    if use_gpu:
        base_options.delegate = BaseOptions.Delegate.GPU
    options = FaceLandmarkerOptions(
        base_options=base_options,
        num_faces=1,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )
    try:
        return FaceLandmarker.create_from_options(options)
    except Exception as exc:
        if use_gpu:
            raise RuntimeError(
                "Failed to initialize the MediaPipe GPU delegate for the face model. "
                "Verify Ubuntu GPU delegate support, drivers, and EGL/OpenGL "
                "availability, or rerun without --gpu."
            ) from exc
        raise


def _create_task_pose_landmarker(*, model_path: str, use_gpu: bool) -> Any:
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.vision.pose_landmarker import (
        PoseLandmarker,
        PoseLandmarkerOptions,
    )

    base_options = BaseOptions(model_asset_path=model_path)
    if use_gpu:
        base_options.delegate = BaseOptions.Delegate.GPU
    options = PoseLandmarkerOptions(
        base_options=base_options,
        num_poses=1,
        output_segmentation_masks=False,
    )
    try:
        return PoseLandmarker.create_from_options(options)
    except Exception as exc:
        if use_gpu:
            raise RuntimeError(
                "Failed to initialize the MediaPipe GPU delegate for the pose model. "
                "Verify Ubuntu GPU delegate support, drivers, and EGL/OpenGL "
                "availability, or rerun without --gpu."
            ) from exc
        raise


def _create_task_hand_landmarker(*, model_path: str, use_gpu: bool) -> Any:
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.vision.hand_landmarker import (
        HandLandmarker,
        HandLandmarkerOptions,
    )

    base_options = BaseOptions(model_asset_path=model_path)
    if use_gpu:
        base_options.delegate = BaseOptions.Delegate.GPU
    options = HandLandmarkerOptions(
        base_options=base_options,
        num_hands=2,
    )
    try:
        return HandLandmarker.create_from_options(options)
    except Exception as exc:
        if use_gpu:
            raise RuntimeError(
                "Failed to initialize the MediaPipe GPU delegate for the hand model. "
                "Verify Ubuntu GPU delegate support, drivers, and EGL/OpenGL "
                "availability, or rerun without --gpu."
            ) from exc
        raise


_POSE_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10),
    (11, 12),
    (11, 13), (13, 15),
    (12, 14), (14, 16),
    (11, 23), (12, 24),
    (23, 24),
    (23, 25), (25, 27),
    (24, 26), (26, 28),
    (27, 29), (29, 31),
    (28, 30), (30, 32),
)

_HAND_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (0, 17), (17, 18), (18, 19), (19, 20),
)


def _draw_tasks_overlay(
    frame_bgr: npt.NDArray[object],
    *,
    result: TrackingResult,
    overlay_lines: tuple[str, ...],
) -> npt.NDArray[object]:
    cv2 = _load_cv2()
    annotated = frame_bgr
    height, width = annotated.shape[:2]
    for landmark in result.face_landmarks:
        center = (int(landmark.x * width), int(landmark.y * height))
        cv2.circle(annotated, center, 1, (255, 255, 0), -1)
    pose_by_index = {landmark.index: landmark for landmark in result.pose_landmarks}
    for start, end in _POSE_CONNECTIONS:
        if start not in pose_by_index or end not in pose_by_index:
            continue
        start_pt = pose_by_index[start]
        end_pt = pose_by_index[end]
        start_xy = (int(start_pt.x * width), int(start_pt.y * height))
        end_xy = (int(end_pt.x * width), int(end_pt.y * height))
        cv2.line(annotated, start_xy, end_xy, (0, 255, 0), 2)
    for landmark in result.pose_landmarks:
        center = (int(landmark.x * width), int(landmark.y * height))
        cv2.circle(annotated, center, 3, (0, 128, 255), -1)
    _draw_hand_overlay(
        annotated,
        hand_landmarks=result.left_hand_landmarks,
        connections=_HAND_CONNECTIONS,
        color=(255, 64, 64),
    )
    _draw_hand_overlay(
        annotated,
        hand_landmarks=result.right_hand_landmarks,
        connections=_HAND_CONNECTIONS,
        color=(64, 64, 255),
    )
    _draw_overlay_lines(annotated, overlay_lines=overlay_lines)
    return annotated


def _draw_hand_overlay(
    frame_bgr: npt.NDArray[object],
    *,
    hand_landmarks: tuple[NormalizedLandmark, ...],
    connections: tuple[tuple[int, int], ...],
    color: tuple[int, int, int],
) -> None:
    cv2 = _load_cv2()
    height, width = frame_bgr.shape[:2]
    hand_by_index = {landmark.index: landmark for landmark in hand_landmarks}
    for start, end in connections:
        if start not in hand_by_index or end not in hand_by_index:
            continue
        start_pt = hand_by_index[start]
        end_pt = hand_by_index[end]
        start_xy = (int(start_pt.x * width), int(start_pt.y * height))
        end_xy = (int(end_pt.x * width), int(end_pt.y * height))
        cv2.line(frame_bgr, start_xy, end_xy, color, 2)
    for landmark in hand_landmarks:
        center = (int(landmark.x * width), int(landmark.y * height))
        cv2.circle(frame_bgr, center, 2, color, -1)


def _split_hand_result(result: object) -> dict[str, dict[str, object | None]]:
    hands: dict[str, dict[str, object | None]] = {
        "left": {"landmarks": None, "world_landmarks": None},
        "right": {"landmarks": None, "world_landmarks": None},
    }
    handedness_sets = list(getattr(result, "handedness", []))
    landmark_sets = list(getattr(result, "hand_landmarks", []))
    world_landmark_sets = list(getattr(result, "hand_world_landmarks", []))
    for index, handedness in enumerate(handedness_sets):
        label = _normalize_handedness_label(handedness)
        if label is None or hands[label]["landmarks"] is not None:
            continue
        hands[label]["landmarks"] = landmark_sets[index] if index < len(landmark_sets) else None
        hands[label]["world_landmarks"] = (
            world_landmark_sets[index] if index < len(world_landmark_sets) else None
        )
    return hands


def _normalize_handedness_label(handedness: object) -> str | None:
    categories = handedness if isinstance(handedness, list) else [handedness]
    for category in categories:
        for attribute_name in ("category_name", "display_name"):
            value = getattr(category, attribute_name, None)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"left", "right"}:
                    return normalized
    return None


def _draw_overlay_lines(
    frame_bgr: npt.NDArray[object],
    *,
    overlay_lines: tuple[str, ...],
) -> None:
    cv2 = _load_cv2()
    for index, line in enumerate(overlay_lines):
        cv2.putText(
            frame_bgr,
            line,
            (20, 30 + index * 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
