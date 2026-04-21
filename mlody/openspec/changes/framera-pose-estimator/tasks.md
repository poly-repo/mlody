# Tasks: framera-pose-estimator

**Change:** framera-pose-estimator **Spec:**
`mlody/openspec/changes/framera-pose-estimator/SPEC.md`
**Requirements:** `mlody/teams/framera/pose_estimation/REQUIREMENTS.md`

---

## Task 1 — OpenSpec and package scaffolding

- Create `mlody/teams/framera/pose_estimation/REQUIREMENTS.md`.
- Create `mlody/openspec/changes/framera-pose-estimator/SPEC.md`.
- Create `mlody/openspec/changes/framera-pose-estimator/tasks.md`.
- Create the `mlody/teams/framera/pose_estimation/` Python package structure and executable shim
  `pose-estimator.py`.

Status: [x]

---

## Task 2 — Calibration and frame schema

- Implement `calibration.py` with typed calibration loading and validation.
- Implement `schema.py` with typed frame and landmark serialization helpers.
- Add unit tests for calibration loading, validation, and JSONL output.

Status: [x]

---

## Task 3 — Geometry helpers

- Implement calibrated pose-camera reconstruction using OpenCV PnP.
- Implement best-effort calibrated face reconstruction with degraded-state
  warnings.
- Add unit tests covering successful transforms and degraded cases.

Status: [x]

---

## Task 4 — CLI and runtime loop

- Implement the Click CLI in `pose_estimator.py`.
- Implement the capture loop, emit-interval gating, and status-frame behavior in
  `runtime.py`.
- Add CLI and runtime-focused tests with mocked capture/inference boundaries.

Status: [x]

---

## Task 5 — MediaPipe and GUI overlay

- Implement `mediapipe_adapter.py` to translate MediaPipe outputs into internal
  dataclasses.
- Add optional GUI overlay rendering via OpenCV.
- Keep GUI optional and stdout-active.

Status: [x]

---

## Task 6 — Dependency and Bazel wiring

- Add `numpy`, `opencv-python`, and `mediapipe` to `pyproject.toml`.
- Run `o-repin`.
- Run `bazel run :gazelle`.
- Ensure `//mlody/teams/framera/pose_estimation/...` builds and tests.

Status: [ ]

Note: `o-repin` completed and `//mlody/teams/framera/pose_estimation:framera_test` plus
`//mlody/teams/framera/pose_estimation:framera_pose_estimator` both succeed. `bazel run
//:gazelle` is currently blocked by unrelated pre-existing repo issues outside
the new `framera` package, so this change keeps a focused hand-authored
`BUILD.bazel` as a temporary fallback.
