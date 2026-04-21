# SPEC: framera Pose Estimator

**Version:** 1.0 **Date:** 2026-04-09 **Architect:** @vitruvius **Status:**
Draft **Requirements:** `mlody/teams/framera/pose_estimation/REQUIREMENTS.md`

---

## Executive Summary

This change introduces a standalone webcam-driven pose_estimation binary for the
`framera` team. The binary reads from a live camera, performs face landmarking
and full-body pose estimation, emits periodic JSONL frames to stdout, and can
optionally display a realtime overlay window.

The design is intentionally split into testable layers: calibration loading,
geometry reconstruction, frame schema serialization, MediaPipe inference, and
the runtime loop. This keeps most of the code unit-testable without a physical
camera while isolating external-library usage behind narrow seams.

---

## Architecture Overview

```
mlody/teams/framera/pose_estimation/
  pose-estimator.py         executable shim requested by stakeholder
  pose_estimator.py         Click CLI entrypoint
  calibration.py            OpenCV-style calibration loading and validation
  geometry.py               camera-space reconstruction helpers
  mediapipe_adapter.py      MediaPipe inference boundary
  runtime.py                capture loop, emit cadence, GUI orchestration
  schema.py                 JSONL frame contracts
  *_test.py                 unit tests
```

Runtime flow:

1. CLI parses camera, output, and calibration options.
2. Calibration is loaded and validated against the requested capture mode.
3. OpenCV opens the capture device and applies width/height/fps settings.
4. MediaPipe processes frames and returns face and pose observations.
5. Geometry helpers convert observations into camera-space metric landmarks.
6. The runtime loop emits JSONL frames on a time interval.
7. If `--gui` is set, the loop also renders a live overlay window.

---

## Technical Stack

- Python 3.13
- Click for CLI parsing
- OpenCV for capture, display, calibration parsing, and PnP solving
- MediaPipe for landmark inference
- NumPy for matrix math
- Bazel with `o_py_binary`, `o_py_library`, `o_py_test`

---

## Detailed Component Specifications

### CLI

- Entry module: `mlody/teams/framera/pose_estimation/pose_estimator.py`
- Executable shim: `mlody/teams/framera/pose_estimation/pose-estimator.py`
- Flags:
  `--device`, `--width`, `--height`, `--fps`, `--emit-interval-ms`,
  `--calibration`, `--gui`

### Calibration

- Accept OpenCV-style JSON and YAML calibration artifacts.
- Required fields:
  camera matrix, distortion coefficients, image width, image height.
- Startup rejects incompatible image size or malformed matrices.

### Inference

- MediaPipe is isolated in `mediapipe_adapter.py`.
- The implementation may prefer MediaPipe Solutions fallback when it provides
  the required pose and face outputs more directly than Tasks APIs.
- v1 tracks only the primary subject.

### Geometry

- Pose camera-space output is derived by solving a PnP transform from
  MediaPipe pose world landmarks to image landmarks using the calibrated camera.
- Face camera-space output is a calibrated best-effort reconstruction based on
  head-pose solving plus face-relative depth.
- Any frame that cannot meet the metric contract is marked degraded with
  warnings; the stream continues.

### Output Contract

- One newline-delimited JSON object per emit interval.
- Fields:
  timestamp, frame size, calibration metadata, status, degraded flag, warnings,
  `face` landmark set, `pose` landmark set.
- Landmark objects include:
  index, x, y, z, and optional visibility/presence.

### GUI

- Optional OpenCV window with the live frame, face contours, pose connectors,
  and lightweight status text.
- The GUI is additive; stdout remains active.

---

## Testing Strategy

- Unit tests for calibration parsing and validation.
- Unit tests for JSON frame serialization.
- Unit tests for geometry degraded-state behavior.
- CLI tests validating option wiring and runtime invocation.
- Small runtime tests for interval gating and status selection.

---

## Risks & Mitigations

- MediaPipe APIs vary across releases.
  Mitigation: isolate imports and external result translation.
- Metric face reconstruction is approximate.
  Mitigation: explicit degraded-state signaling and warnings.
- Camera/display behavior varies by machine.
  Mitigation: keep GUI optional and test most logic without hardware.

---

## Implementation Notes

- Add `numpy`, `opencv-python`, and `mediapipe` to `pyproject.toml`.
- Run `o-repin` after dependency changes.
- Run `bazel run :gazelle` after creating the new Python files.
