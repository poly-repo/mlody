"""Tests for StructuredError — all scenarios from the structured-error spec."""

from __future__ import annotations

import pytest

from common.python.errors import StructuredError


def test_structured_error_defaults_exit_code_to_1() -> None:
    # Scenario: StructuredError constructed with message only
    error = StructuredError("something went wrong")

    assert error.exit_code == 1


def test_structured_error_sets_message() -> None:
    # Scenario: StructuredError constructed with message only
    error = StructuredError("something went wrong")

    assert error.message == "something went wrong"


def test_structured_error_defaults_context_to_empty_dict() -> None:
    # Scenario: StructuredError constructed with message only
    error = StructuredError("something went wrong")

    assert error.context == {}


def test_structured_error_str_equals_message() -> None:
    # Scenario: StructuredError constructed with message only
    error = StructuredError("something went wrong")

    assert str(error) == "something went wrong"


def test_structured_error_explicit_exit_code_is_stored() -> None:
    # Scenario: StructuredError constructed with explicit exit_code
    error = StructuredError("build failed", exit_code=3)

    assert error.exit_code == 3
    assert error.message == "build failed"


def test_structured_error_kwargs_become_context_dict() -> None:
    # Scenario: StructuredError constructed with context kwargs
    error = StructuredError("clone failed", exit_code=2, remote="origin", target="//foo")

    assert error.context == {"remote": "origin", "target": "//foo"}


def test_structured_error_is_catchable_as_exception() -> None:
    # Scenario: StructuredError is catchable as Exception
    with pytest.raises(Exception):
        raise StructuredError("test error")


def test_structured_error_is_catchable_as_structured_error() -> None:
    # Scenario: StructuredError is catchable as Exception
    with pytest.raises(StructuredError):
        raise StructuredError("test error")
