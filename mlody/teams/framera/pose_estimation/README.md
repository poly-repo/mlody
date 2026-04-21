wget   -O ~/.cache/mlody/artifacts/google/mediapipe/holistic_landmarker.task   https://storage.googleapis.com/mediapipe-models/holistic_landmarker/holistic_landmarker/float16/latest/holistic_landmarker.task
curl -L -o ~/.cache/mlody/artifacts/google/mediapipe/face_landmarker.task   https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
curl -L -o ~/.cache/mlody/artifacts/google/mediapipe/pose_landmarker_full.task   https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task
curl -L   -o ~/.cache/mlody/artifacts/google/mediapipe/hand_landmarker.task   https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task

mlody/teams/framera/pose_estimation/framera_pose_estimator_nvidia --body --no-json --gpu --face-model ~/.cache/mlody/artifacts/google/mediapipe/face_landmarker.task --pose-model ~/.cache/mlody/artifacts/google/mediapipe/pose_landmarker_full.task --calibration $(pwd)/mlody/teams/framera/pose_estimation/camera.json --width 640 --height 480 --device 1 --gui

mlody/teams/framera/pose_estimation/framera_pose_estimator_nvidia --body --hands --no-json --gpu --hand-model ~/.cache/mlody/artifacts/google/mediapipe/hand_landmarker.task --face-model ~/.cache/mlody/artifacts/google/mediapipe/face_landmarker.task --pose-model ~/.cache/mlody/artifacts/google/mediapipe/pose_landmarker_full.task --calibration $(pwd)/mlody/teams/framera/pose_estimation/camera.json --width 640 --height 480 --device 1 --gui
