"""Runtime loop for Framera YOLOv26 webcam inference tasks."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mlody.teams.framera.yolo26.schema import Detection, FramePacket

TaskName = Literal["detection", "segmentation"]
SUPPORTED_TASKS: tuple[TaskName, ...] = ("detection", "segmentation")
TASK_MODEL_SUFFIX_BY_NAME: dict[TaskName, str] = {
    "detection": "",
    "segmentation": "-seg",
}

MODEL_CACHE_DIR = Path(
    "~/.cache/mlody/artifacts/huggingface/Ultralytics/YOLOv26/de6601a0e3575f8968a22a383021d1099d990857"
).expanduser()
DEFAULT_MODEL_SIZE = "x"
GREEN_BGR = (0, 255, 0)


@dataclass(frozen=True)
class SessionConfig:
    """Runtime configuration for one camera session."""

    task: TaskName
    device: int
    width: int
    height: int
    fps: int
    emit_interval_ms: int
    emit_json: bool
    gui: bool
    gpu: bool
    model_path: Path
    conf: float
    iou: float
    max_det: int
    isolate_class: int | None = None


def default_model_path_for_task(*, task: TaskName) -> Path:
    suffix = TASK_MODEL_SUFFIX_BY_NAME[task]
    return MODEL_CACHE_DIR / f"yolo26{DEFAULT_MODEL_SIZE}{suffix}.pt"


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


def choose_status(*, detection_count: int) -> str:
    """Select a stream status string."""
    if detection_count == 0:
        return "no_detection"
    return "ok"


def build_overlay_lines(
    *,
    task: TaskName,
    status: str,
    detection_count: int,
    segment_count: int | None,
    isolate_class: int | None,
    isolate_hit: bool | None,
    fps: float,
    model_name: str,
) -> tuple[str, ...]:
    """Build the overlay text lines shown in the GUI."""
    lines: list[str] = [
        f"Task: {task}",
        f"Status: {status}",
        f"Detections: {detection_count}",
    ]
    if segment_count is not None:
        lines.append(f"Segments: {segment_count}")
    if isolate_class is not None:
        lines.append(f"Isolate class: {isolate_class}")
    if isolate_hit is not None:
        lines.append(f"Isolate hit: {'yes' if isolate_hit else 'no'}")
    lines.extend((f"FPS: {fps:.1f}", f"Model: {model_name}"))
    return tuple(lines)


def run_camera_session(*, config: SessionConfig) -> None:
    """Run the capture, inference, emit, and optional GUI loop."""
    cv2 = _load_cv2()
    capture = _open_capture(config=config, cv2=cv2)
    gate = EmissionGate(interval_seconds=config.emit_interval_ms / 1000.0)
    fps_counter = FpsCounter()
    model, class_names, inference_device = _load_detector(config=config)

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                time.sleep(0.01)
                continue

            frame_height, frame_width = frame.shape[:2]
            result = _predict_frame(
                model=model,
                frame=frame,
                conf=config.conf,
                iou=config.iou,
                max_det=config.max_det,
                device=inference_device,
            )
            detections = _extract_detections(result=result, class_names=class_names)
            segmentation_masks = (
                _extract_segmentation_masks(result=result, detection_count=len(detections))
                if config.task == "segmentation"
                else ()
            )
            segmentation_polygons = (
                _extract_segmentation_polygons(result=result, detection_count=len(detections))
                if config.task == "segmentation"
                else ()
            )
            status = choose_status(detection_count=len(detections))

            now = time.monotonic()
            if config.emit_json and gate.should_emit(now):
                packet = FramePacket(
                    timestamp_ms=int(time.time() * 1000),
                    frame_width=frame_width,
                    frame_height=frame_height,
                    task=config.task,
                    status=status,
                    model=config.model_path.name,
                    detections=detections,
                )
                print(packet.to_json_line(), flush=True)

            if config.gui:
                fps = fps_counter.update(now)
                annotated, isolate_hit = _draw_overlay(
                    cv2=cv2,
                    frame=frame,
                    detections=detections,
                    segmentation_masks=segmentation_masks,
                    segmentation_polygons=segmentation_polygons,
                    isolate_class=config.isolate_class,
                    use_gpu_blend=config.gpu,
                    overlay_lines=(),
                )
                _draw_overlay_lines(
                    cv2=cv2,
                    frame=annotated,
                    overlay_lines=build_overlay_lines(
                        task=config.task,
                        status=status,
                        detection_count=len(detections),
                        segment_count=(
                            max(len(segmentation_masks), len(segmentation_polygons))
                            if config.task == "segmentation"
                            else None
                        ),
                        isolate_class=config.isolate_class,
                        isolate_hit=isolate_hit,
                        fps=fps,
                        model_name=config.model_path.name,
                    ),
                )
                cv2.imshow("framera yolo26 detector", annotated)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
    finally:
        capture.release()
        if config.gui:
            cv2.destroyAllWindows()


def _load_detector(*, config: SessionConfig) -> tuple[Any, dict[int, str], str]:
    try:
        import torch
        from ultralytics import YOLO
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "ultralytics is required for YOLOv26 inference. Add it to runtime dependencies."
        ) from exc

    inference_device = "cuda:0" if config.gpu and torch.cuda.is_available() else "cpu"
    model = YOLO(str(config.model_path))
    names = _normalize_class_names(getattr(model, "names", {}))
    return model, names, inference_device


def _predict_frame(
    *,
    model: Any,
    frame: Any,
    conf: float,
    iou: float,
    max_det: int,
    device: str,
) -> Any:
    results = model.predict(
        source=frame,
        conf=conf,
        iou=iou,
        max_det=max_det,
        device=device,
        verbose=False,
    )
    if not results:
        raise RuntimeError("YOLO prediction returned no results.")
    return results[0]


def _extract_detections(
    *,
    result: Any,
    class_names: dict[int, str],
) -> tuple[Detection, ...]:
    boxes = getattr(result, "boxes", None)
    if boxes is None:
        return ()

    xyxy_rows = _to_rows(getattr(boxes, "xyxy", ()))
    if not xyxy_rows:
        return ()
    confidences = _to_vector(getattr(boxes, "conf", ()))
    class_ids = _to_vector(getattr(boxes, "cls", ()))
    count = min(len(xyxy_rows), len(confidences), len(class_ids))
    detections: list[Detection] = []

    for index in range(count):
        x1, y1, x2, y2 = xyxy_rows[index]
        class_id = int(class_ids[index])
        detections.append(
            Detection(
                index=index,
                class_id=class_id,
                class_name=class_names.get(class_id, str(class_id)),
                confidence=float(confidences[index]),
                x1=float(x1),
                y1=float(y1),
                x2=float(x2),
                y2=float(y2),
            )
        )

    return tuple(detections)


def _extract_segmentation_masks(
    *,
    result: Any,
    detection_count: int,
) -> tuple[Any, ...]:
    masks = getattr(result, "masks", None)
    if masks is None:
        return ()
    mask_data = getattr(masks, "data", None)
    if mask_data is None:
        return ()
    masks_array = _to_numpy_array(mask_data)
    if masks_array.ndim != 3:
        return ()
    count = min(detection_count, int(masks_array.shape[0]))
    return tuple(masks_array[index] for index in range(count))


def _extract_segmentation_polygons(
    *,
    result: Any,
    detection_count: int,
) -> tuple[Any, ...]:
    masks = getattr(result, "masks", None)
    if masks is None:
        return ()
    polygons = getattr(masks, "xy", None)
    if polygons is None:
        return ()
    count = min(detection_count, len(polygons))
    return tuple(polygons[index] for index in range(count))


def _draw_overlay(
    *,
    cv2: Any,
    frame: Any,
    detections: tuple[Detection, ...],
    segmentation_masks: tuple[Any, ...],
    segmentation_polygons: tuple[Any, ...],
    isolate_class: int | None,
    use_gpu_blend: bool,
    overlay_lines: tuple[str, ...],
) -> tuple[Any, bool | None]:
    isolate_hit: bool | None = None
    detections_to_draw = detections
    if isolate_class is not None:
        annotated, isolate_hit = _apply_isolation_mask(
            cv2=cv2,
            frame=frame,
            detections=detections,
            segmentation_masks=segmentation_masks,
            segmentation_polygons=segmentation_polygons,
            target_class=isolate_class,
        )
        detections_to_draw = tuple(
            detection for detection in detections if detection.class_id == isolate_class
        )
    else:
        annotated = _apply_segmentation_layers(
            cv2=cv2,
            base_frame=frame,
            detections=detections,
            segmentation_masks=segmentation_masks,
            segmentation_polygons=segmentation_polygons,
            alpha=0.35,
            use_gpu_blend=use_gpu_blend,
        )
    frame_height, frame_width = annotated.shape[:2]
    for detection in detections_to_draw:
        x1, y1, x2, y2 = _clip_box(
            detection=detection,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        if x2 <= x1 or y2 <= y1:
            continue
        color = _color_for_class(detection.class_id)
        label = _format_detection_label(detection)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        _draw_label(
            cv2=cv2,
            frame=annotated,
            x=x1,
            y=y1,
            label=label,
            color=color,
            frame_width=frame_width,
        )

    _draw_overlay_lines(cv2=cv2, frame=annotated, overlay_lines=overlay_lines)
    return annotated, isolate_hit


def _draw_overlay_lines(
    *,
    cv2: Any,
    frame: Any,
    overlay_lines: tuple[str, ...],
) -> None:
    for index, line in enumerate(overlay_lines):
        cv2.putText(
            frame,
            line,
            (10, 30 + (index * 30)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )


def _apply_segmentation_layers(
    *,
    cv2: Any,
    base_frame: Any,
    detections: tuple[Detection, ...],
    segmentation_masks: tuple[Any, ...],
    segmentation_polygons: tuple[Any, ...],
    alpha: float,
    use_gpu_blend: bool,
) -> Any:
    import numpy as np

    if not segmentation_masks and not segmentation_polygons:
        return base_frame.copy()

    frame_height, frame_width = base_frame.shape[:2]
    overlay = base_frame.copy()
    applied = False
    for detection, raw_mask in zip(detections, segmentation_masks):
        mask = _prepare_mask_for_frame(
            cv2=cv2,
            raw_mask=raw_mask,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        if mask is None or not mask.any():
            continue
        color = np.asarray(_color_for_class(detection.class_id), dtype=np.uint8)
        overlay[mask] = color
        applied = True

    if not applied and segmentation_polygons:
        for detection, raw_polygon in zip(detections, segmentation_polygons):
            polygon = _sanitize_polygon(
                raw_polygon=raw_polygon,
                frame_width=frame_width,
                frame_height=frame_height,
            )
            if polygon is None:
                continue
            color = tuple(int(channel) for channel in _color_for_class(detection.class_id))
            cv2.fillPoly(overlay, [polygon], color)
            applied = True

    if not applied:
        return base_frame.copy()

    return _alpha_blend(
        cv2=cv2,
        base_frame=base_frame,
        overlay_frame=overlay,
        alpha=alpha,
        use_gpu_blend=use_gpu_blend,
    )


def _apply_isolation_mask(
    *,
    cv2: Any,
    frame: Any,
    detections: tuple[Detection, ...],
    segmentation_masks: tuple[Any, ...],
    segmentation_polygons: tuple[Any, ...],
    target_class: int,
) -> tuple[Any, bool]:
    import numpy as np

    frame_height, frame_width = frame.shape[:2]
    combined = np.zeros((frame_height, frame_width), dtype=bool)

    for detection, raw_mask in zip(detections, segmentation_masks):
        if detection.class_id != target_class:
            continue
        mask = _prepare_mask_for_frame(
            cv2=cv2,
            raw_mask=raw_mask,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        if mask is None:
            continue
        combined |= mask

    if not combined.any() and segmentation_polygons:
        polygon_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
        for detection, raw_polygon in zip(detections, segmentation_polygons):
            if detection.class_id != target_class:
                continue
            polygon = _sanitize_polygon(
                raw_polygon=raw_polygon,
                frame_width=frame_width,
                frame_height=frame_height,
            )
            if polygon is None:
                continue
            cv2.fillPoly(polygon_mask, [polygon], 1)
        combined = polygon_mask.astype(bool)

    has_target = bool(combined.any())
    isolated = np.empty_like(frame)
    isolated[:, :] = GREEN_BGR
    if has_target:
        isolated[combined] = frame[combined]
    return isolated, has_target


def _prepare_mask_for_frame(
    *,
    cv2: Any,
    raw_mask: Any,
    frame_width: int,
    frame_height: int,
) -> Any | None:
    mask = _to_numpy_array(raw_mask)
    if mask.ndim != 2:
        return None
    if mask.shape != (frame_height, frame_width):
        mask = cv2.resize(mask, (frame_width, frame_height), interpolation=cv2.INTER_LINEAR)
    return mask > 0.5


def _sanitize_polygon(
    *,
    raw_polygon: Any,
    frame_width: int,
    frame_height: int,
) -> Any | None:
    import numpy as np

    polygon = _to_numpy_array(raw_polygon)
    if polygon.ndim != 2 or polygon.shape[1] != 2 or polygon.shape[0] < 3:
        return None
    x = np.clip(np.round(polygon[:, 0]), 0, max(frame_width - 1, 0))
    y = np.clip(np.round(polygon[:, 1]), 0, max(frame_height - 1, 0))
    return np.stack([x, y], axis=1).astype(np.int32)


def _alpha_blend(
    *,
    cv2: Any,
    base_frame: Any,
    overlay_frame: Any,
    alpha: float,
    use_gpu_blend: bool,
) -> Any:
    if use_gpu_blend and _has_cuda_add_weighted(cv2=cv2):
        try:
            base_gpu = cv2.cuda_GpuMat()
            overlay_gpu = cv2.cuda_GpuMat()
            base_gpu.upload(base_frame)
            overlay_gpu.upload(overlay_frame)
            blended_gpu = cv2.cuda.addWeighted(
                overlay_gpu,
                alpha,
                base_gpu,
                1.0 - alpha,
                0.0,
            )
            return blended_gpu.download()
        except Exception:
            pass
    return cv2.addWeighted(overlay_frame, alpha, base_frame, 1.0 - alpha, 0.0)


def _has_cuda_add_weighted(*, cv2: Any) -> bool:
    cuda = getattr(cv2, "cuda", None)
    if cuda is None:
        return False
    if not hasattr(cv2, "cuda_GpuMat"):
        return False
    if not hasattr(cuda, "addWeighted"):
        return False
    if not hasattr(cuda, "getCudaEnabledDeviceCount"):
        return False
    try:
        return cuda.getCudaEnabledDeviceCount() > 0
    except Exception:
        return False


def _draw_label(
    *,
    cv2: Any,
    frame: Any,
    x: int,
    y: int,
    label: str,
    color: tuple[int, int, int],
    frame_width: int,
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.6
    thickness = 2
    text_size, baseline = cv2.getTextSize(label, font, font_scale, thickness)
    text_width, text_height = text_size
    top = max(0, y - text_height - baseline - 8)
    bottom = top + text_height + baseline + 6
    right = min(frame_width - 1, x + text_width + 8)
    cv2.rectangle(frame, (x, top), (right, bottom), color, -1)
    cv2.putText(
        frame,
        label,
        (x + 4, bottom - baseline - 2),
        font,
        font_scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )


def _format_detection_label(detection: Detection) -> str:
    return f"{detection.class_name} {detection.confidence:.2f}"


def _clip_box(
    *,
    detection: Detection,
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int]:
    max_x = max(frame_width - 1, 0)
    max_y = max(frame_height - 1, 0)
    x1 = min(max(int(round(detection.x1)), 0), max_x)
    y1 = min(max(int(round(detection.y1)), 0), max_y)
    x2 = min(max(int(round(detection.x2)), 0), max_x)
    y2 = min(max(int(round(detection.y2)), 0), max_y)
    return x1, y1, x2, y2


def _color_for_class(class_id: int) -> tuple[int, int, int]:
    # Deterministic BGR pseudo-random palette based on class id.
    return (
        (37 * (class_id + 1)) % 256,
        (17 * (class_id + 5)) % 256,
        (29 * (class_id + 3)) % 256,
    )


def _open_capture(*, config: SessionConfig, cv2: Any) -> Any:
    capture = cv2.VideoCapture(config.device)
    if not capture.isOpened():
        raise RuntimeError(f"failed to open camera device {config.device}")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(config.width))
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(config.height))
    capture.set(cv2.CAP_PROP_FPS, float(config.fps))
    return capture


def _normalize_class_names(raw_names: Any) -> dict[int, str]:
    if isinstance(raw_names, dict):
        return {int(key): str(value) for key, value in raw_names.items()}
    if isinstance(raw_names, list):
        return {index: str(value) for index, value in enumerate(raw_names)}
    return {}


def _to_rows(raw: Any) -> list[list[float]]:
    value = _to_python_list(raw)
    return [list(row) for row in value]


def _to_vector(raw: Any) -> list[float]:
    value = _to_python_list(raw)
    return [float(item) for item in value]


def _to_numpy_array(raw: Any) -> Any:
    value = raw
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return value.numpy()
    try:
        import numpy as np
    except ModuleNotFoundError:
        return value
    return np.asarray(value)


def _to_python_list(raw: Any) -> list[Any]:
    value = raw
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    return []


def _load_cv2() -> Any:
    import cv2

    return cv2
