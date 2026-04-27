"""Tests for __main__.py CLI — click command validation and error handling."""

from __future__ import annotations

import json
from unittest.mock import patch

import mlody
import pytest
from click.testing import CliRunner

from mlody.common.image_builder.__main__ import main
from mlody.common.image_builder.errors import BazelBuildError, ExitCode
from mlody.common.image_builder.output import SuccessResult

assert mlody.__name__ == "mlody"

_VALID_SHA = "a" * 40
_REGISTRY = "registry.example.com/mlody"
_TARGET = "//mlody/lsp:lsp_server"


def test_invalid_sha_exits_with_clone_failure_code() -> None:
    runner = CliRunner()
    result = runner.invoke(main, [_TARGET, "--sha", "tooshort", "--registry", _REGISTRY])
    assert result.exit_code == ExitCode.CLONE_FAILURE


def test_invalid_sha_emits_json_error_to_stdout() -> None:
    runner = CliRunner()
    result = runner.invoke(main, [_TARGET, "--sha", "tooshort", "--registry", _REGISTRY])
    parsed = json.loads(result.output)
    assert parsed["error"] == "validation"
    assert "40-digit" in parsed["message"]


def test_valid_inputs_with_mocked_run_exits_zero() -> None:
    success = SuccessResult(
        image_digest="sha256:abc",
        image_references=[f"{_REGISTRY}:some-tag"],
        commit_sha=_VALID_SHA,
        input_targets=[_TARGET],
    )
    runner = CliRunner()
    with patch("mlody.common.image_builder.__main__.run", return_value=success):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    assert result.exit_code == ExitCode.SUCCESS


def test_valid_inputs_with_mocked_run_emits_json_success() -> None:
    success = SuccessResult(
        image_digest="sha256:abc",
        image_references=[f"{_REGISTRY}:some-tag"],
        commit_sha=_VALID_SHA,
        input_targets=[_TARGET],
    )
    runner = CliRunner()
    with patch("mlody.common.image_builder.__main__.run", return_value=success):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    parsed = json.loads(result.output)
    assert parsed["image_digest"] == "sha256:abc"
    assert parsed["commit_sha"] == _VALID_SHA


def test_bazel_build_error_exits_with_build_failure_code() -> None:
    runner = CliRunner()
    with patch(
        "mlody.common.image_builder.__main__.run",
        side_effect=BazelBuildError("bazel failed", returncode=1),
    ):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    assert result.exit_code == ExitCode.BUILD_FAILURE


def test_bazel_build_error_emits_json_error_to_stdout() -> None:
    runner = CliRunner()
    with patch(
        "mlody.common.image_builder.__main__.run",
        side_effect=BazelBuildError("bazel failed", returncode=1),
    ):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    parsed = json.loads(result.output)
    assert parsed["error"] == "BazelBuildError"
    assert parsed["message"] == "bazel failed"


def test_unexpected_exception_exits_with_code_1() -> None:
    runner = CliRunner()
    with patch(
        "mlody.common.image_builder.__main__.run",
        side_effect=RuntimeError("something unexpected"),
    ):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    assert result.exit_code == 1


def test_unexpected_exception_emits_json_error_to_stdout() -> None:
    runner = CliRunner()
    with patch(
        "mlody.common.image_builder.__main__.run",
        side_effect=RuntimeError("something unexpected"),
    ):
        result = runner.invoke(
            main,
            [_TARGET, "--sha", _VALID_SHA, "--registry", _REGISTRY],
        )
    parsed = json.loads(result.output)
    assert parsed["error"] == "UnexpectedError"
    assert "unexpected" in parsed["message"]
