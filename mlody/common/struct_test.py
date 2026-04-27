"""Tests for the mlody-owned Struct compatibility layer."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "mlody_struct_under_test",
    Path(__file__).with_name("struct.py"),
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
Struct = _MODULE.Struct
struct = _MODULE.struct


class TestStructCompatibilityLayer:
    """Requirement: mlody.common.struct extends the shared Struct API in place."""

    def test_struct_aliases_starlarkish_struct(self) -> None:
        assert Struct.__module__ == "common.python.starlarkish.core.struct"

    def test_get_returns_existing_field_value(self) -> None:
        value = Struct(answer=42)

        assert value.get("answer") == 42

    def test_get_returns_default_for_missing_field(self) -> None:
        value = Struct(answer=42)

        assert value.get("missing", "fallback") == "fallback"

    def test_items_matches_mapping_items(self) -> None:
        value = Struct(answer=42, label="ok")

        assert list(value.items()) == [("answer", 42), ("label", "ok")]

    def test_field_named_items_still_resolves_to_field_value(self) -> None:
        value = Struct(items=[1, 2, 3])

        assert value.items == [1, 2, 3]

    def test_updated_returns_new_struct_without_mutating_original(self) -> None:
        value = Struct(answer=42, label="before")

        updated = value.updated(label="after", status="ok")

        assert updated is not value
        assert updated.label == "after"
        assert updated.status == "ok"
        assert value.label == "before"
        assert value.get("status") is None

    def test_struct_factory_preserves_existing_to_dict_behavior(self) -> None:
        value = struct(config={"answer": 42}, values=[{"label": "x"}])

        assert value.to_dict() == {
            "config": {"answer": 42},
            "values": [{"label": "x"}],
        }

    def test_as_mapping_remains_read_only(self) -> None:
        value = Struct(answer=42)

        mapping = value.as_mapping()

        with pytest.raises(TypeError):
            mapping["answer"] = 7  # type: ignore[index]
