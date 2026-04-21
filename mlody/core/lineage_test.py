"""Tests for mlody.core.lineage."""

from __future__ import annotations

from starlarkish.core.struct import Struct

from mlody.core.lineage import append_lineage, build_lineage_event


class TestLineageHelpers:
    """Unit tests for lineage event construction and append semantics."""

    def test_build_lineage_event_returns_struct_with_expected_fields(self) -> None:
        """Task 5.1: the event builder produces the canonical struct shape."""
        event = build_lineage_event(
            accessor=".config",
            new_value=42,
            author="tester",
            reason="update",
            timestamp="2026-04-20T00:00:00Z",
            mode="copy",
        )

        assert event.kind == "lineage_event"
        assert event.accessor == ".config"
        assert event.new_value == 42
        assert event.author == "tester"
        assert event.reason == "update"
        assert event.timestamp == "2026-04-20T00:00:00Z"
        assert event.mode == "copy"

    def test_append_lineage_rebuilds_struct_with_appended_event(self) -> None:
        """Task 5.2: append_lineage preserves old lineage and appends one event."""
        value = Struct(name="x", _lineage=["old"])
        event = build_lineage_event(
            accessor=".name",
            new_value="y",
            author=None,
            reason=None,
            timestamp=None,
            mode="inplace",
        )

        updated = append_lineage(value, event, mode="inplace")

        assert updated._lineage == ["old", event]
