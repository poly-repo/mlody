"""Tests for F9 — BuilderError inherits StructuredError."""

from __future__ import annotations

from common.python.errors import StructuredError
from mlody.common.image_builder.errors import (
    BazelBuildError,
    BuilderError,
    CloneError,
    ExitCode,
)


def test_clone_error_is_instance_of_structured_error() -> None:
    # Scenario: BuilderError is a StructuredError
    error = CloneError("clone failed", remote="r")

    assert isinstance(error, StructuredError)


def test_clone_error_has_correct_exit_code() -> None:
    # Scenario: BuilderError is a StructuredError — exit_code == 2 (ExitCode.CLONE_FAILURE)
    error = CloneError("clone failed", remote="r")

    assert error.exit_code == 2


def test_clone_error_context_contains_kwargs() -> None:
    # Scenario: BuilderError is a StructuredError — context carries kwargs
    error = CloneError("clone failed", remote="r")

    assert error.context == {"remote": "r"}


def test_exit_code_enum_values_unchanged() -> None:
    # Scenario: ExitCode enum remains available
    assert ExitCode.SUCCESS == 0
    assert ExitCode.CLONE_FAILURE == 2
    assert ExitCode.BUILD_FAILURE == 3
    assert ExitCode.PUSH_FAILURE == 4


def test_bazel_build_error_is_catchable_as_structured_error() -> None:
    # Scenario: BuilderError subclasses are catchable as StructuredError
    try:
        raise BazelBuildError("build failed", targets=["//foo"])
    except StructuredError as e:
        assert e.exit_code == 3


def test_builder_error_is_subclass_of_structured_error() -> None:
    assert issubclass(BuilderError, StructuredError)
