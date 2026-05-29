"""Tests for mlody.core.lineage."""

from __future__ import annotations

from common.python.starlarkish.core.struct import Struct

from mlody.common.value import RegisteredValue
from mlody.core.assets.manifest import cache_key_for_uri
from mlody.core.lineage import (
    LineageEvent,
    append_lineage,
    build_lineage_event,
    materialized_lineage,
    record_lineage,
    remember_runtime_label,
)
from mlody.db.assets import record_observation, upsert_blob, upsert_external_asset
from mlody.db.evaluations import open_db


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

    def test_materialized_lineage_reads_all_versions_for_canonical_owner_label(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        default_value = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="foo"),
        )
        stale_config_value = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="FOOBAR"),
        )
        remember_runtime_label(stale_config_value, "//simple:a-string")
        stale_config_event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", type="inline", data="FOOBAR"),
            source="CONFIG: xxx: //simple:a-string=FOOBAR",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        assert record_lineage(stale_config_value, stale_config_event) is True

        stale_command_line_value = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="bar"),
        )
        remember_runtime_label(stale_command_line_value, "//simple:a-string")
        stale_command_line_event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", type="inline", data="bar"),
            source="COMMAND_LINE: //simple:a-string=bar",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        assert record_lineage(stale_command_line_value, stale_command_line_event) is True

        remember_runtime_label(default_value, "@workspace//simple:a-string")
        default_event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", type="inline", data="foo"),
            source="DEFAULT: foo",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        assert record_lineage(default_value, default_event) is True

        config_value = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="FOOBAR"),
        )
        remember_runtime_label(config_value, "//simple:a-string")
        config_event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", type="inline", data="FOOBAR"),
            source="CONFIG: xxx: //simple:a-string=FOOBAR",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        assert record_lineage(
            config_value,
            config_event,
            previous_owner=default_value,
        ) is True

        command_line_value = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="bar"),
        )
        command_line_event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", type="inline", data="bar"),
            source="COMMAND_LINE: //simple:a-string=bar",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        remember_runtime_label(command_line_value, "//simple:a-string")
        assert record_lineage(
            command_line_value,
            command_line_event,
            previous_owner=config_value,
        ) is True

        fresh = Struct(
            kind="value",
            name="a-string",
            location=Struct(kind="location", type="inline", data="bar"),
        )
        remember_runtime_label(fresh, "//simple:a-string")

        persisted = materialized_lineage(fresh)

        assert [event.source for event in persisted] == [
            "DEFAULT: foo",
            "CONFIG: xxx: //simple:a-string=FOOBAR",
            "COMMAND_LINE: //simple:a-string=bar",
        ]
        assert [event.new_value.data for event in persisted] == ["foo", "FOOBAR", "bar"]

    def test_materialized_lineage_reads_persisted_remote_event_from_db(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        db_path = tmp_path / "mlody.sqlite"
        cached_path = tmp_path / "employees.csv"
        cached_path.write_text("name,salary\nAlice,120000\n")
        uri = "https://example.com/employees.csv"
        owner_label = "@demo//datasets:employees"

        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: db_path,
        )

        original = Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": uri},
            ),
        )
        remember_runtime_label(original, owner_label)

        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data=uri),
            source="downloaded from",
            reason=None,
            timestamp=None,
            mode="inplace",
            details={
                "kind": "remote-download",
                "uri": uri,
                "staged_path": str(cached_path),
                "content_hash": "abc123",
                "location": {
                    "kind": "location",
                    "type": "remote",
                    "attributes": {"uri": uri},
                },
            },
        )
        assert record_lineage(original, event) is True

        conn = open_db(db_path)
        try:
            asset_id = upsert_external_asset(
                conn,
                uri=uri,
                transport="http",
                cache_key=cache_key_for_uri(uri),
                value_name="employees",
            )
            upsert_blob(
                conn,
                content_hash="abc123",
                local_path=str(cached_path),
                size_bytes=cached_path.stat().st_size,
            )
            record_observation(
                conn,
                asset_id=asset_id,
                blob_sha="abc123",
                status="downloaded",
                content_length=cached_path.stat().st_size,
                resolved_url=uri,
            )
        finally:
            conn.close()

        fresh = Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": uri},
            ),
        )
        remember_runtime_label(fresh, owner_label)

        persisted = materialized_lineage(fresh)

        assert len(persisted) == 1
        assert persisted[0].source == "downloaded from"
        assert persisted[0].details["content_hash"] == "abc123"
