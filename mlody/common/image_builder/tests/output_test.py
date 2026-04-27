"""Tests for output.py — JSON serialization to stdout."""

from __future__ import annotations

import io
import json
import sys

import mlody
import pytest

from mlody.common.image_builder.output import SuccessResult, emit_error, emit_success

assert mlody.__name__ == "mlody"


def _capture_stdout(fn: object, *args: object) -> str:
    """Capture stdout produced by calling fn(*args)."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn(*args)  # type: ignore[operator]
    finally:
        sys.stdout = old
    return buf.getvalue()


def _capture_stderr(fn: object, *args: object) -> str:
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        fn(*args)  # type: ignore[operator]
    finally:
        sys.stderr = old
    return buf.getvalue()


def test_emit_success_writes_valid_json_to_stdout() -> None:
    result = SuccessResult(
        image_digest="sha256:abc123",
        image_references=["registry.example.com/mlody:some-tag"],
        commit_sha="a" * 40,
        input_targets=["//mlody/lsp:lsp_server"],
    )
    output = _capture_stdout(emit_success, result)
    parsed = json.loads(output)
    assert parsed["image_digest"] == "sha256:abc123"
    assert parsed["commit_sha"] == "a" * 40
    assert parsed["input_targets"] == ["//mlody/lsp:lsp_server"]
    assert parsed["image_references"] == ["registry.example.com/mlody:some-tag"]


def test_emit_success_contains_all_required_fields() -> None:
    result = SuccessResult(
        image_digest="sha256:def456",
        image_references=["reg:tag1", "reg:tag2"],
        commit_sha="b" * 40,
        input_targets=["//a:b", "//c:d"],
    )
    output = _capture_stdout(emit_success, result)
    parsed = json.loads(output)
    required_fields = {"image_digest", "image_references", "commit_sha", "input_targets"}
    assert required_fields.issubset(parsed.keys())


def test_emit_error_writes_valid_json_to_stdout() -> None:
    output = _capture_stdout(
        emit_error, "BazelBuildError", "build failed", {"returncode": 1, "targets": ["//a:b"]}
    )
    parsed = json.loads(output)
    assert parsed["error"] == "BazelBuildError"
    assert parsed["message"] == "build failed"
    assert parsed["returncode"] == 1


def test_emit_error_includes_error_and_message_keys() -> None:
    output = _capture_stdout(emit_error, "CloneError", "clone failed", {})
    parsed = json.loads(output)
    assert "error" in parsed
    assert "message" in parsed


def test_emit_success_does_not_write_to_stderr() -> None:
    result = SuccessResult(
        image_digest="sha256:abc",
        image_references=[],
        commit_sha="c" * 40,
        input_targets=[],
    )
    stderr_output = _capture_stderr(emit_success, result)
    assert stderr_output == ""


def test_emit_error_does_not_write_to_stderr() -> None:
    stderr_output = _capture_stderr(emit_error, "SomeError", "msg", {})
    assert stderr_output == ""
