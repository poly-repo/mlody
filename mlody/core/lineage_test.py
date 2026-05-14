"""Tests for mlody.core.lineage."""

from __future__ import annotations

from common.python.starlarkish.core.struct import Struct

from mlody.common.value import RegisteredValue
from mlody.core.lineage import (
    LineageEvent,
    append_lineage,
    build_lineage_event,
    record_lineage,
)


class TestLineageHelpers:
    """Unit tests for lineage event construction and append semantics."""

    def test_build_lineage_event_returns_dataclass_with_expected_fields(self) -> None:
        """Task 5.1: the event builder produces the canonical lineage shape."""
        event = build_lineage_event(
            accessor=".config",
            new_value=42,
            source="tester",
            reason="update",
            timestamp="2026-04-20T00:00:00Z",
            mode="copy",
        )

        assert isinstance(event, LineageEvent)
        assert event.kind == "lineage_event"
        assert event.accessor == ".config"
        assert event.new_value == 42
        assert event.source == "tester"
        assert event.reason == "update"
        assert event.timestamp == "2026-04-20T00:00:00Z"
        assert event.mode == "copy"
        assert event.details is None

    def test_build_lineage_event_preserves_optional_details_payload(self) -> None:
        event = build_lineage_event(
            accessor=".location",
            new_value="source.csv",
            source="downloaded from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={"uri": "https://example.com/source.csv"},
        )

        assert event.details == {"uri": "https://example.com/source.csv"}

    def test_append_lineage_rebuilds_struct_with_appended_event(self) -> None:
        """Task 5.2: append_lineage preserves old lineage and appends one event."""
        value = Struct(name="x", _lineage=["old"])
        event = build_lineage_event(
            accessor=".name",
            new_value="y",
            source=None,
            reason=None,
            timestamp=None,
            mode="inplace",
        )

        updated = append_lineage(value, event, mode="inplace")

        assert updated._lineage == ["old", event]

    def test_append_lineage_rebuilds_registered_value_with_appended_event(self) -> None:
        """Registered value wrappers preserve their type when lineage is appended."""
        value = RegisteredValue(
            Struct(
                kind="value",
                name="artifact",
                type=Struct(kind="type", name="string"),
                location=Struct(kind="location", type="inline"),
                freshness=Struct(kind="freshness", type="always"),
                _lineage=["old"],
            )
        )
        event = build_lineage_event(
            accessor=".source",
            new_value="new",
            source="tester",
            reason="override",
            timestamp="2026-05-05T00:00:00Z",
            mode="copy",
        )

        updated = append_lineage(value, event, mode="copy")

        assert isinstance(updated, RegisteredValue)
        assert updated._lineage == ["old", event]
        assert updated.name == "artifact"

    def test_record_lineage_mutates_existing_lineage_list_in_place(self) -> None:
        event = build_lineage_event(
            accessor=".location",
            new_value="upstream",
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        value = Struct(name="artifact", _lineage=[])

        recorded = record_lineage(value, event)

        assert recorded is True
        assert value._lineage == [event]

    def test_record_lineage_dedupes_equal_events(self) -> None:
        event = build_lineage_event(
            accessor=".location",
            new_value="upstream",
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        value = Struct(name="artifact", _lineage=[])

        first = record_lineage(value, event)
        second = record_lineage(value, event)

        assert first is True
        assert second is False
        assert value._lineage == [event]
