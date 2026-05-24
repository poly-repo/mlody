"""Tests for phases/build.py — bazel build invocation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import mlody
import pytest
from pyfakefs import fake_filesystem as _fake_filesystem

from mlody.common.image_builder.errors import BazelBuildError
from mlody.common.image_builder.phases.build import BazelResult, _DYN_PKG, run_bazel_build
from mlody.common.image_builder.phases.clone import CloneResult

assert mlody.__name__ == "mlody"
assert _fake_filesystem.__name__ == "pyfakefs.fake_filesystem"

_SHA = "a" * 40
_CLONE_DIR = Path("/fake/clone/dir")
_TARGETS = ["//mlody/lsp:lsp_server", "//mlody/core:worker"]
_PY_TARGETS = ["//mlody/cli:mlody", "//repo/smoketest/python/simple:simple"]


def _clean_result(path: Path = _CLONE_DIR) -> CloneResult:
    return CloneResult(path=path, applied_patch="", applied_untracked=[])


def _dirty_result(patch: str = "diff --git a/f.py b/f.py\n") -> CloneResult:
    return CloneResult(path=_CLONE_DIR, applied_patch=patch, applied_untracked=["new.py"])


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


def _fake_run_no_python(args: list[str], **kwargs: object) -> MagicMock:
    """subprocess.run side_effect: query returns no Python targets; build succeeds."""
    if "query" in args:
        return _make_subprocess_result(returncode=1)
    return _make_subprocess_result()


def _fake_run_all_python(targets: list[str]) -> object:
    """Return a side_effect that marks all targets as Python binaries."""

    def _inner(args: list[str], **kwargs: object) -> MagicMock:
        if "query" in args:
            return _make_subprocess_result(stdout="\n".join(targets))
        return _make_subprocess_result()

    return _inner


def test_run_bazel_build_returns_bazel_result_on_success(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        if "query" in args:
            return _make_subprocess_result(returncode=1)
        return _make_subprocess_result(stdout="build output", stderr="")

    with patch("mlody.common.image_builder.phases.build.subprocess.run", side_effect=fake_run):
        result = run_bazel_build(_SHA, _clean_result(), _TARGETS)

    assert isinstance(result, BazelResult)
    assert result.stdout == "build output"


def test_run_bazel_build_raises_on_nonzero_exit(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        if "query" in args:
            return _make_subprocess_result(returncode=1)
        return _make_subprocess_result(returncode=1, stderr="ERROR: build failed")

    with patch("mlody.common.image_builder.phases.build.subprocess.run", side_effect=fake_run):
        with pytest.raises(BazelBuildError) as exc_info:
            run_bazel_build(_SHA, _clean_result(), _TARGETS)

    assert exc_info.value.context["returncode"] == 1
    assert "ERROR: build failed" in str(exc_info.value.context["stderr"])


def test_run_bazel_build_writes_build_bazel_with_pkg_tar_for_non_python(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    with patch(
        "mlody.common.image_builder.phases.build.subprocess.run",
        side_effect=_fake_run_no_python,
    ):
        run_bazel_build(_SHA, _clean_result(), _TARGETS)

    build_file = _CLONE_DIR / _DYN_PKG / "BUILD.bazel"
    assert build_file.exists()
    content = build_file.read_text()
    for target in _TARGETS:
        assert target in content
    assert "oci_image" in content
    assert "pkg_tar" in content
    assert "py_image_layer" not in content


def test_run_bazel_build_uses_py_image_layer_for_python_targets(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    with patch(
        "mlody.common.image_builder.phases.build.subprocess.run",
        side_effect=_fake_run_all_python(_PY_TARGETS),
    ):
        run_bazel_build(_SHA, _clean_result(), _PY_TARGETS)

    content = (_CLONE_DIR / _DYN_PKG / "BUILD.bazel").read_text()
    for target in _PY_TARGETS:
        assert target in content
    assert "py_image_layer" in content
    assert "pkg_tar" not in content
    assert "oci_image" in content


def test_run_bazel_build_mixes_py_image_layer_and_pkg_tar(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]
    mixed = [_PY_TARGETS[0], _TARGETS[0]]
    py_only = [_PY_TARGETS[0]]

    with patch(
        "mlody.common.image_builder.phases.build.subprocess.run",
        side_effect=_fake_run_all_python(py_only),
    ):
        run_bazel_build(_SHA, _clean_result(), mixed)

    content = (_CLONE_DIR / _DYN_PKG / "BUILD.bazel").read_text()
    assert "py_image_layer" in content
    assert "pkg_tar" in content
    for label in mixed:
        assert label in content


def test_run_bazel_build_includes_revision_label(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    with patch(
        "mlody.common.image_builder.phases.build.subprocess.run",
        side_effect=_fake_run_no_python,
    ):
        run_bazel_build(_SHA, _clean_result(), _TARGETS)

    content = (_CLONE_DIR / _DYN_PKG / "BUILD.bazel").read_text()
    assert "org.opencontainers.image.revision" in content
    assert _SHA in content
    assert 'com.polymath.mlody.dirty": "false"' in content


def test_run_bazel_build_marks_dirty_label_when_patch_applied(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    with patch(
        "mlody.common.image_builder.phases.build.subprocess.run",
        side_effect=_fake_run_no_python,
    ):
        run_bazel_build(_SHA, _dirty_result(), _TARGETS)

    content = (_CLONE_DIR / _DYN_PKG / "BUILD.bazel").read_text()
    assert 'com.polymath.mlody.dirty": "true"' in content
    assert "com.polymath.mlody.dirty_files_changed" in content
    assert "com.polymath.mlody.dirty_untracked" in content


def test_run_bazel_build_invokes_correct_target(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]
    captured_calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        captured_calls.append(args)
        if "query" in args:
            return _make_subprocess_result(returncode=1)
        return _make_subprocess_result()

    with patch("mlody.common.image_builder.phases.build.subprocess.run", side_effect=fake_run):
        run_bazel_build(_SHA, _clean_result(), _TARGETS)

    build_calls = [c for c in captured_calls if "build" in c]
    assert len(build_calls) == 1
    assert f"//{_DYN_PKG}:all" in build_calls[0]


def test_run_bazel_build_error_contains_targets(fs: object) -> None:
    _CLONE_DIR.mkdir(parents=True)  # type: ignore[union-attr]

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        if "query" in args:
            return _make_subprocess_result(returncode=1)
        return _make_subprocess_result(returncode=1, stderr="")

    with patch("mlody.common.image_builder.phases.build.subprocess.run", side_effect=fake_run):
        with pytest.raises(BazelBuildError) as exc_info:
            run_bazel_build(_SHA, _clean_result(), _TARGETS)

    assert exc_info.value.context["targets"] == _TARGETS
