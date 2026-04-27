"""Tests for errors.py — exit code mapping and error hierarchy."""

from __future__ import annotations

import mlody

from mlody.common.image_builder.errors import (
    BazelBuildError,
    BuilderError,
    CloneError,
    ExitCode,
    PushError,
)

assert mlody.__name__ == "mlody"


def test_clone_error_has_clone_failure_exit_code() -> None:
    err = CloneError("some clone failure", sha="abc123")
    assert err.exit_code == ExitCode.CLONE_FAILURE


def test_bazel_build_error_has_build_failure_exit_code() -> None:
    err = BazelBuildError("bazel failed", returncode=1)
    assert err.exit_code == ExitCode.BUILD_FAILURE


def test_push_error_has_push_failure_exit_code() -> None:
    err = PushError("push failed", tag="some-tag")
    assert err.exit_code == ExitCode.PUSH_FAILURE


def test_all_exit_codes_are_distinct_and_nonzero() -> None:
    codes = [ExitCode.CLONE_FAILURE, ExitCode.BUILD_FAILURE, ExitCode.PUSH_FAILURE]
    assert len(set(codes)) == 3
    assert all(c != 0 for c in codes)


def test_builder_error_subclasses_are_exceptions() -> None:
    err = CloneError("msg")
    assert isinstance(err, BuilderError)
    assert isinstance(err, Exception)


def test_builder_error_stores_context_kwargs() -> None:
    err = CloneError("msg", sha="abc", lock="/tmp/abc.lock")
    assert err.context["sha"] == "abc"
    assert err.context["lock"] == "/tmp/abc.lock"


def test_builder_error_message_accessible() -> None:
    err = BazelBuildError("build broke", returncode=2)
    assert err.message == "build broke"
    assert str(err) == "build broke"
