"""Tests for Framera YOLOv26 runtime helpers."""

from __future__ import annotations

import cv2
import numpy as np

from mlody.teams.framera.yolo26.runtime import (
    GREEN_BGR,
    EmissionGate,
    FpsCounter,
    _apply_isolation_mask,
    build_overlay_lines,
    choose_status,
    default_model_path_for_task,
    _extract_segmentation_polygons,
    _extract_segmentation_masks,
    _clip_box,
    _extract_detections,
    _format_detection_label,
)
from mlody.teams.framera.yolo26.schema import Detection


class _FakeTensor:
    def __init__(self, values: list[float] | list[list[float]]) -> None:
        self._values = values

    def cpu(self) -> "_FakeTensor":
        return self

    def tolist(self) -> list[float] | list[list[float]]:
        return self._values


class _FakeBoxes:
    def __init__(self) -> None:
        self.xyxy = _FakeTensor([[1.0, 2.0, 10.0, 20.0], [4.0, 5.0, 14.0, 25.0]])
        self.conf = _FakeTensor([0.9, 0.6])
        self.cls = _FakeTensor([1.0, 3.0])


class _FakeResult:
    def __init__(self, boxes: object) -> None:
        self.boxes = boxes


class _FakeNumpyTensor:
    def __init__(self, value: np.ndarray) -> None:
        self._value = value

    def detach(self) -> "_FakeNumpyTensor":
        return self

    def cpu(self) -> "_FakeNumpyTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._value


class _FakeMasks:
    def __init__(self, data: np.ndarray, polygons: list[np.ndarray] | None = None) -> None:
        self.data = _FakeNumpyTensor(data)
        self.xy = polygons or []


class _FakeSegmentationResult:
    def __init__(self, data: np.ndarray | None, polygons: list[np.ndarray] | None = None) -> None:
        self.masks = _FakeMasks(data, polygons=polygons) if data is not None else None


def test_emission_gate_allows_first_emit_and_respects_interval() -> None:
    gate = EmissionGate(interval_seconds=0.5)

    assert gate.should_emit(0.0) is True
    assert gate.should_emit(0.2) is False
    assert gate.should_emit(0.6) is True


def test_choose_status_and_overlay_lines() -> None:
    assert choose_status(detection_count=0) == "no_detection"
    assert choose_status(detection_count=1) == "ok"
    assert build_overlay_lines(
        task="detection",
        status="ok",
        detection_count=2,
        segment_count=None,
        isolate_class=None,
        isolate_hit=None,
        fps=12.34,
        model_name="yolo26x.pt",
    ) == (
        "Task: detection",
        "Status: ok",
        "Detections: 2",
        "FPS: 12.3",
        "Model: yolo26x.pt",
    )


def test_fps_counter_smooths_frame_rate_updates() -> None:
    counter = FpsCounter()

    assert counter.update(0.0) == 0.0
    assert round(counter.update(0.1), 1) == 10.0
    assert round(counter.update(0.2), 1) == 10.0


def test_extract_detections_maps_boxes_to_schema() -> None:
    detections = _extract_detections(
        result=_FakeResult(_FakeBoxes()),
        class_names={1: "person", 3: "car"},
    )

    assert len(detections) == 2
    assert detections[0].class_name == "person"
    assert detections[0].confidence == 0.9
    assert detections[1].class_id == 3
    assert detections[1].x2 == 14.0


def test_extract_detections_handles_missing_boxes() -> None:
    detections = _extract_detections(
        result=_FakeResult(None),
        class_names={},
    )

    assert detections == ()


def test_format_detection_label_and_box_clipping() -> None:
    detection = Detection(
        index=0,
        class_id=2,
        class_name="bicycle",
        confidence=0.876,
        x1=-5.2,
        y1=10.1,
        x2=999.9,
        y2=50.6,
    )

    assert _format_detection_label(detection) == "bicycle 0.88"
    assert _clip_box(
        detection=detection,
        frame_width=640,
        frame_height=480,
    ) == (0, 10, 639, 51)


def test_default_model_path_for_task_uses_suffix_mapping() -> None:
    assert default_model_path_for_task(task="detection").name == "yolo26x.pt"
    assert default_model_path_for_task(task="segmentation").name == "yolo26x-seg.pt"


def test_extract_segmentation_masks_limits_to_detection_count() -> None:
    result = _FakeSegmentationResult(
        np.array(
            [
                [[1.0, 0.0], [0.0, 1.0]],
                [[0.0, 1.0], [1.0, 0.0]],
            ],
            dtype=np.float32,
        )
    )

    masks = _extract_segmentation_masks(result=result, detection_count=1)

    assert len(masks) == 1
    assert masks[0].shape == (2, 2)


def test_extract_segmentation_masks_handles_missing_masks() -> None:
    result = _FakeSegmentationResult(None)

    masks = _extract_segmentation_masks(result=result, detection_count=3)

    assert masks == ()


def test_extract_segmentation_polygons_limits_to_detection_count() -> None:
    data = np.array([[[1.0]]], dtype=np.float32)
    polygons = [
        np.array([[1.0, 2.0], [10.0, 2.0], [10.0, 12.0]], dtype=np.float32),
        np.array([[3.0, 4.0], [12.0, 4.0], [12.0, 14.0]], dtype=np.float32),
    ]
    result = _FakeSegmentationResult(data, polygons=polygons)

    extracted = _extract_segmentation_polygons(result=result, detection_count=1)

    assert len(extracted) == 1
    assert extracted[0].shape == (3, 2)


def test_apply_isolation_mask_keeps_target_and_greens_background() -> None:
    frame = np.array(
        [
            [[10, 20, 30], [40, 50, 60]],
            [[70, 80, 90], [100, 110, 120]],
        ],
        dtype=np.uint8,
    )
    detections = (
        Detection(index=0, class_id=0, class_name="person", confidence=0.9, x1=0, y1=0, x2=1, y2=1),
    )
    segmentation_masks = (
        np.array(
            [
                [1.0, 0.0],
                [0.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )

    isolated, has_target = _apply_isolation_mask(
        cv2=cv2,
        frame=frame,
        detections=detections,
        segmentation_masks=segmentation_masks,
        segmentation_polygons=(),
        target_class=0,
    )

    assert has_target is True
    assert tuple(isolated[0, 0]) == tuple(frame[0, 0])
    assert tuple(isolated[0, 1]) == GREEN_BGR
    assert tuple(isolated[1, 0]) == GREEN_BGR
    assert tuple(isolated[1, 1]) == GREEN_BGR


def test_apply_isolation_mask_greens_all_when_target_missing() -> None:
    frame = np.array(
        [
            [[1, 2, 3], [4, 5, 6]],
            [[7, 8, 9], [10, 11, 12]],
        ],
        dtype=np.uint8,
    )
    detections = (
        Detection(index=0, class_id=2, class_name="car", confidence=0.9, x1=0, y1=0, x2=1, y2=1),
    )
    segmentation_masks = (
        np.array(
            [
                [1.0, 1.0],
                [1.0, 1.0],
            ],
            dtype=np.float32,
        ),
    )

    isolated, has_target = _apply_isolation_mask(
        cv2=cv2,
        frame=frame,
        detections=detections,
        segmentation_masks=segmentation_masks,
        segmentation_polygons=(),
        target_class=0,
    )

    assert has_target is False
    assert (isolated == np.array(GREEN_BGR, dtype=np.uint8)).all(axis=2).all()
