# Requirements Document: framera Pose Estimator

**Version:** 1.0 **Date:** 2026-04-09 **Prepared by:** Requirements Analyst AI
(@socrates) **Status:** Draft

---

## 1. Executive Summary

`framera` is a standalone webcam-driven Python tool for realtime face landmark
tracking and body pose estimation. It reads from a local video camera, emits
newline-delimited JSON frames to stdout, and can optionally open a desktop
window showing the live feed with landmarks and connectors overlaid.

The primary value is to provide a developer-friendly capture utility that
produces machine-consumable landmark streams while remaining useful for live
inspection and debugging. The system is optimized for local laptop usage,
single-subject capture, and calibrated metric output over raw framerate.

---

## 2. Project Scope

### 2.1 In Scope

- Standalone `o_py_binary` under `mlody/teams/framera/pose_estimation/`.
- Live webcam input only.
- Single primary subject.
- Face landmarks plus full-body pose landmarks.
- Periodic JSONL frames on stdout with 3D landmark payloads.
- Optional `--gui` overlay window using the live camera feed.
- OpenCV-style calibration input for metric camera-space reconstruction.
- Explicit degraded-status signaling when metric output cannot be trusted.

### 2.2 Out of Scope

- Pre-recorded video input.
- Multi-person tracking.
- Hand landmarks.
- Recording to disk.
- Hard realtime guarantees.
- Calibration workflow generation inside the binary.

### 2.3 Assumptions

- The caller runs on a local developer machine with one webcam available.
- The caller can provide a valid calibration file when metric output is needed.
- Accuracy is more important than latency in v1.

### 2.4 Constraints

- Python 3.13, basedpyright strict mode, ruff formatting.
- Bazel targets use `o_py_library`, `o_py_binary`, and `o_py_test`.
- `pose-estimator.py` must exist as the binary entrypoint path requested by the
  stakeholder.

---

## 3. Stakeholders

| Role | Name/Group | Responsibilities |
|------|------------|------------------|
| Primary user | Framera developers | Run the tool locally and consume landmark streams |
| Requirements Analyst | @socrates | Capture and document requirements |
| Solution Architect | @vitruvius | Produce the implementation-ready specification |
| Implementation | @vulcan-python | Deliver tested Python code |

---

## 4. Business Requirements

- **BR-001:** Provide a simple local tool for realtime facial and pose
  extraction from a webcam.
- **BR-002:** Produce machine-consumable stdout output suitable for piping into
  downstream tooling.
- **BR-003:** Support visual verification through an optional GUI overlay.
- **BR-004:** Prefer calibrated metric 3D fidelity over maximum framerate.

---

## 5. User Requirements

- **US-001:** As a developer, I want to point the tool at a webcam and receive
  JSON frames with face and pose landmarks.
- **US-002:** As a developer, I want `--gui` to show the live camera feed with
  landmarks and skeleton connectors overlaid.
- **US-003:** As a developer, I want the stdout stream to continue with explicit
  status frames even when detection is temporarily lost.
- **US-004:** As a developer, I want calibration metadata and confidence fields
  in each frame so downstream consumers can decide whether to accept it.

---

## 6. Functional Requirements

- **FR-001:** The binary accepts flags for camera device, width, height, fps,
  emit interval, calibration path, and `--gui`.
- **FR-002:** The binary captures frames from a live webcam only.
- **FR-003:** The system estimates face landmarks and full-body pose landmarks
  for a single primary subject.
- **FR-004:** The system emits one JSON object per configured time interval.
- **FR-005:** Each JSON frame includes timestamp, frame metadata, calibration
  metadata, detection status, degraded-state information, face landmarks, and
  pose landmarks.
- **FR-006:** `--gui` opens a blocking realtime overlay window while stdout
  emission continues.
- **FR-007:** Invalid or incompatible calibration input fails fast at startup.
- **FR-008:** If metric 3D cannot be trusted for a frame, the frame is marked as
  degraded rather than silently falling back.

---

## 7. Non-Functional Requirements

- **NFR-001:** Best-effort usable realtime on a local laptop.
- **NFR-002:** Accuracy over latency.
- **NFR-003:** Clear, typed Python interfaces with unit-testable boundaries.
- **NFR-004:** Headless operation is supported when `--gui` is not used.

---

## 8. Data Requirements

- Landmark payloads use stable set names (`face`, `pose`) and MediaPipe indices.
- 3D output is camera-space metric coordinates when calibration and runtime
  conditions support it.
- Missing detections are represented explicitly in the stream, not by omission
  of frames.

---

## 9. Integration Requirements

- OpenCV for capture, calibration parsing, and optional display.
- MediaPipe for face and pose landmark inference.
- No external services or network APIs at runtime.

---

## 10. Testing & Acceptance

- Calibrated happy path produces JSONL frames with face and pose landmarks.
- `--gui` renders a live overlay window without disabling stdout output.
- Invalid calibration input fails clearly.
- Missing detections emit explicit status frames.
- Non-default camera settings are passed through correctly.
