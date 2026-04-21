"""Tests for mlody.core.location_composition.

All test names trace back to scenarios in:
  openspec/changes/mlody-field-traversal/specs/field-traversal/spec.md
"""

from __future__ import annotations

from pathlib import Path

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.location_composition import (
    _LOCATION_COMPOSERS,
    _LocationComposeError,
    compose_location,
    register_location_composer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _posix_loc(path: str | list[str], name: str = "loc") -> Struct:
    return Struct(kind="posix", type="posix", name=name, path=path)


# ---------------------------------------------------------------------------
# Requirement: compose_location — FR-008 rules
# ---------------------------------------------------------------------------


class TestComposeLocationRules:
    """Requirement: compose_location implements FR-008 composition rules."""

    def test_both_none_returns_none(self) -> None:
        """Scenario: Both locations None returns None."""
        result = compose_location(None, None, "weights")
        assert result is None

    def test_parent_none_field_present_returns_field_loc(self) -> None:
        """Scenario: Parent None, field location present returns field location."""
        field_loc = _posix_loc("models/bert/config")
        result = compose_location(None, field_loc, "weights")
        assert result is field_loc

    def test_parent_present_field_none_appends_field_name(self) -> None:
        """Scenario: Parent present, field None appends field name to parent path."""
        parent_loc = _posix_loc("foo/bar")
        result = compose_location(parent_loc, None, "model_info")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == ["foo/bar/model_info"]
        assert getattr(result, "kind", None) == "location"

    def test_same_kind_delegates_to_registered_handler(self) -> None:
        """Scenario: Both present, same kind delegates to registered handler."""
        parent_loc = _posix_loc("models/bert")
        field_loc = _posix_loc("config")
        result = compose_location(parent_loc, field_loc, "config")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == ["models/bert/config"]

    def test_different_kinds_raises_location_compose_error(self) -> None:
        """Scenario: Both present, different kinds raises _LocationComposeError."""
        parent_loc = Struct(kind="posix", path="foo")
        field_loc = Struct(kind="s3", path="bar")
        with pytest.raises(_LocationComposeError) as exc_info:
            compose_location(parent_loc, field_loc, "weights")
        msg = str(exc_info.value)
        assert "posix" in msg
        assert "s3" in msg
        assert "weights" in msg
        assert "cross-kind" in msg

    def test_unregistered_parent_kind_raises_location_compose_error(self) -> None:
        """Scenario: Unregistered parent kind raises _LocationComposeError."""
        parent_loc = Struct(kind="gcs", path="bucket/models")
        field_loc = Struct(kind="gcs", path="weights")
        with pytest.raises(_LocationComposeError) as exc_info:
            compose_location(parent_loc, field_loc, "weights")
        msg = str(exc_info.value)
        assert "gcs" in msg


# ---------------------------------------------------------------------------
# Requirement: _LOCATION_COMPOSERS dispatch table and registration API
# ---------------------------------------------------------------------------


class TestLocationComposersDispatchTable:
    """Requirement: _LOCATION_COMPOSERS dispatch table and registration API."""

    def test_posix_handler_registered_at_import(self) -> None:
        """Scenario: posix handler registered at import time."""
        assert "posix" in _LOCATION_COMPOSERS
        assert callable(_LOCATION_COMPOSERS["posix"])

    def test_register_location_composer_adds_and_is_invoked(self) -> None:
        """Scenario: Registered handler is invoked for matching kind."""
        calls: list[tuple[object, object, str]] = []

        def mock_fn(parent: Struct, field: Struct | None, name: str) -> Struct:
            calls.append((parent, field, name))
            return Struct(kind="location", type="mock_kind", name="composed", path="mock")

        register_location_composer("mock_kind", mock_fn)
        try:
            parent_loc = Struct(kind="mock_kind", path="p")
            field_loc = Struct(kind="mock_kind", path="f")
            result = compose_location(parent_loc, field_loc, "my_field")
        finally:
            del _LOCATION_COMPOSERS["mock_kind"]

        assert len(calls) == 1
        assert calls[0][2] == "my_field"
        assert isinstance(result, Struct)

    def test_register_location_composer_replaces_existing_handler(self) -> None:
        """Scenario: Registration replaces existing handler."""
        original_fn = _LOCATION_COMPOSERS.get("posix")
        assert original_fn is not None

        sentinel = Struct(kind="location", type="posix", name="sentinel", path="sentinel")

        def replacement_fn(parent: Struct, field: Struct | None, name: str) -> Struct:
            return sentinel

        register_location_composer("posix", replacement_fn)
        try:
            assert _LOCATION_COMPOSERS["posix"] is replacement_fn
            parent_loc = _posix_loc("any/path")
            result = compose_location(parent_loc, None, "field")
            assert result is sentinel
        finally:
            # Restore original posix handler to avoid polluting other tests.
            register_location_composer("posix", original_fn)

        # Confirm original is restored.
        assert _LOCATION_COMPOSERS["posix"] is original_fn


# ---------------------------------------------------------------------------
# Requirement: posix location composition handler
# ---------------------------------------------------------------------------


class TestPosixHandler:
    """Requirement: posix location composition handler semantics."""

    def test_posix_handler_joins_parent_path_and_field_path(self) -> None:
        """Scenario: posix handler joins parent path and field path."""
        parent_loc = _posix_loc("models/bert", name="parent_loc")
        field_loc = _posix_loc("config", name="field_loc")
        result = compose_location(parent_loc, field_loc, "config")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == ["models/bert/config"]
        assert getattr(result, "kind", None) == "location"

    def test_posix_handler_appends_field_name_when_field_loc_absent(self) -> None:
        """Scenario: posix handler appends field name when field_loc path absent."""
        parent_loc = _posix_loc("models/bert", name="parent_loc")
        result = compose_location(parent_loc, None, "weights")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == ["models/bert/weights"]

    def test_posix_handler_preserves_parent_name(self) -> None:
        """Composed location carries the parent's name attribute."""
        parent_loc = _posix_loc("models", name="my_named_loc")
        result = compose_location(parent_loc, None, "weights")
        assert isinstance(result, Struct)
        assert getattr(result, "name", None) == "my_named_loc"

    def test_posix_handler_with_both_locs_returns_joined_path(self) -> None:
        """Both parent and field locs present, same posix kind → paths are joined."""
        parent_loc = _posix_loc("data/raw")
        field_loc = _posix_loc("train_split")
        result = compose_location(parent_loc, field_loc, "train_split")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == ["data/raw/train_split"]

    def test_posix_handler_supports_path_lists(self) -> None:
        """List paths compose using cartesian join semantics."""
        parent_loc = _posix_loc(["root/a", "root/b"])
        field_loc = _posix_loc(["x", "y"])
        result = compose_location(parent_loc, field_loc, "ignored_name")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == [
            "root/a/x",
            "root/a/y",
            "root/b/x",
            "root/b/y",
        ]

    def test_posix_handler_expands_glob_patterns(self, tmp_path: Path) -> None:
        """Glob patterns are expanded after parent/field path concatenation."""
        data_root = tmp_path / "data"
        data_root.mkdir()
        (data_root / "train-0001.parquet").write_text("x")
        (data_root / "train-0002.parquet").write_text("y")
        (data_root / "test-0001.parquet").write_text("z")

        parent_loc = _posix_loc(str(data_root))
        field_loc = _posix_loc("train-*")
        result = compose_location(parent_loc, field_loc, "ignored_name")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == [
            str(data_root / "train-0001.parquet"),
            str(data_root / "train-0002.parquet"),
        ]

    def test_posix_handler_expands_glob_patterns_under_tilde_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Glob expansion resolves '~' in parent paths before matching."""
        monkeypatch.setenv("HOME", str(tmp_path))
        data_root = tmp_path / ".cache" / "glob-home"
        data_root.mkdir(parents=True)
        (data_root / "train-0001.parquet").write_text("x")
        (data_root / "train-0002.parquet").write_text("y")

        parent_loc = _posix_loc("~/.cache/glob-home")
        field_loc = _posix_loc("train-000*")
        result = compose_location(parent_loc, field_loc, "ignored_name")
        assert isinstance(result, Struct)
        assert getattr(result, "path", None) == [
            str(data_root / "train-0001.parquet"),
            str(data_root / "train-0002.parquet"),
        ]
