"""Tests for mlody.core.lineage."""

from __future__ import annotations

from common.python.starlarkish.core.struct import Struct

from mlody.common.value import RegisteredValue
from mlody.core.lineage import (
    LineageEvent,
    append_lineage,
    build_lineage_event,
    materialized_lineage,
    record_lineage,
    remember_runtime_label,
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

    def test_append_lineage_persists_struct_event_in_db(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """Task 5.2: append_lineage persists one event for an identified value."""
        value = Struct(
            name="x",
            location=Struct(kind="location", type="inline", data="old"),
        )
        event = build_lineage_event(
            accessor=".name",
            new_value="y",
            source=None,
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value, "@demo//entities:x")

        updated = append_lineage(value, event, mode="inplace")

        assert updated is value
        persisted = materialized_lineage(value)
        assert len(persisted) == 1
        assert persisted[0].accessor == event.accessor
        assert persisted[0].new_value == event.new_value
        assert persisted[0].source == event.source

    def test_append_lineage_preserves_registered_value_wrapper(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """Registered value wrappers stay typed while lineage is persisted externally."""
        value = RegisteredValue(
            Struct(
                kind="value",
                name="artifact",
                type=Struct(kind="type", name="string"),
                location=Struct(kind="location", type="inline", data="old"),
                freshness=Struct(kind="freshness", type="always"),
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
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value, "@demo//entities:artifact")

        updated = append_lineage(value, event, mode="copy")

        assert isinstance(updated, RegisteredValue)
        persisted = materialized_lineage(updated)
        assert len(persisted) == 1
        assert persisted[0].accessor == event.accessor
        assert persisted[0].new_value == event.new_value
        assert persisted[0].source == event.source
        assert updated.name == "artifact"

    def test_record_lineage_persists_one_event(self, tmp_path, monkeypatch) -> None:
        event = build_lineage_event(
            accessor=".location",
            new_value="upstream",
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        value = Struct(
            name="artifact",
            location=Struct(kind="location", type="inline", data="artifact"),
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value, "@demo//artifacts:artifact")

        recorded = record_lineage(value, event)

        assert recorded is True
        persisted = materialized_lineage(value)
        assert len(persisted) == 1
        assert persisted[0].accessor == event.accessor
        assert persisted[0].new_value == event.new_value
        assert persisted[0].source == event.source

    def test_record_lineage_dedupes_equal_events(self, tmp_path, monkeypatch) -> None:
        event = build_lineage_event(
            accessor=".location",
            new_value="upstream",
            source="copied from",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        value = Struct(
            name="artifact",
            location=Struct(kind="location", type="inline", data="artifact"),
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value, "@demo//artifacts:artifact")

        first = record_lineage(value, event)
        second = record_lineage(value, event)

        assert first is True
        assert second is False
        persisted = materialized_lineage(value)
        assert len(persisted) == 1
        assert persisted[0].accessor == event.accessor
        assert persisted[0].new_value == event.new_value
        assert persisted[0].source == event.source
