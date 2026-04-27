"""Regression tests for mlody.core.virtual_value traversal helpers."""

from __future__ import annotations

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.virtual_value import step_object


class TestStepObject:
    """Requirement: step_object preserves list-by-name traversal semantics."""

    def test_step_object_selects_named_item_from_list(self) -> None:
        items = [
            Struct(name="features", kind="value"),
            Struct(name="labels", kind="value"),
        ]

        result = step_object(items, "labels")

        assert getattr(result, "name", None) == "labels"

    def test_step_object_raises_key_error_for_missing_named_item(self) -> None:
        items = [Struct(name="features", kind="value")]

        with pytest.raises(KeyError, match="labels"):
            step_object(items, "labels")

    def test_step_object_falls_back_to_getattr_for_non_lists(self) -> None:
        result = step_object(Struct(answer=42), "answer")

        assert result == 42
