"""Regression tests for mlody.core.virtual_value traversal helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.lineage import (
    build_lineage_event,
    record_lineage,
    remember_runtime_label,
)
from mlody.core.virtual_value import lookup_runtime_attribute, step_object


def _remote_asset(path: Path, *, uri: str, content_hash: str) -> MaterializedAsset:
    return MaterializedAsset(
        path=path,
        content_hash=content_hash,
        metadata=AssetMetadata(
            uri=uri,
            resolved_url=uri,
            digest=None,
            digest_type=None,
            length=None,
            update_time=None,
            transport="http",
        ),
    )


def _path_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


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


class TestLineageVirtualAttribute:
    def test_lineage_attribute_reads_persisted_events_when_memory_is_empty(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        lineage_type = Struct(
            kind="type",
            name="vector",
            _root_kind="vector",
            attributes={
                "element_type": Struct(
                    kind="type",
                    name="mlody-lineage-event",
                    _root_kind="record",
                    attributes={},
                    _allowed_attrs={},
                )
            },
            _allowed_attrs={},
        )
        value_struct = Struct(
            kind="value",
            name="run_config",
            type=Struct(
                kind="type",
                name="mlody-value",
                virtual_attributes=[Struct(name="lineage", type=lineage_type)],
                _allowed_attrs={},
            ),
            location=Struct(
                kind="location",
                type="inline",
                data=Struct(batch_size=32, enabled=True),
            ),
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value_struct, "@demo//training:run_config")

        event = build_lineage_event(
            accessor=".location",
            new_value=Struct(kind="location", data="32"),
            source="DEFAULT: 32",
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        assert record_lineage(value_struct, event) is True

        lineage_attr = lookup_runtime_attribute(value_struct, "lineage")

        assert lineage_attr is not None
        lineage = lineage_attr.materializer(value_struct)

        assert len(lineage) == 1
        assert lineage[0].source == "DEFAULT: 32"
        assert lineage[0].new_value.data == "32"

    def test_lineage_attribute_populates_remote_download_lineage(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        lineage_type = Struct(
            kind="type",
            name="vector",
            _root_kind="vector",
            attributes={
                "element_type": Struct(
                    kind="type",
                    name="mlody-lineage-event",
                    _root_kind="record",
                    attributes={},
                    _allowed_attrs={},
                )
            },
            _allowed_attrs={},
        )
        value_struct = Struct(
            kind="value",
            name="employees",
            type=Struct(
                kind="type",
                name="mlody-value",
                virtual_attributes=[Struct(name="lineage", type=lineage_type)],
                _allowed_attrs={},
            ),
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/data.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value_struct, "@demo//datasets:employees")

        lineage_attr = lookup_runtime_attribute(value_struct, "lineage")

        assert lineage_attr is not None
        with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
            mock_materialize.return_value = _remote_asset(
                Path("/tmp/staged.csv"),
                uri="https://example.com/data.csv",
                content_hash="abc123",
            )

            lineage = lineage_attr.materializer(value_struct)

        assert len(lineage) == 1
        assert lineage[0].source == "downloaded from"
        assert lineage[0].new_value.data == "https://example.com/data.csv"
        assert lineage[0].details["staged_path"] == "/tmp/staged.csv"

    def test_lineage_attribute_materializes_source_backed_local_copy(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        staged_path = tmp_path / "staged.csv"
        staged_path.write_text("name,salary\nAlice,120000\n")
        content_hash = _path_hash(staged_path)
        destination_path = tmp_path / "cache" / "employees.csv"
        lineage_type = Struct(
            kind="type",
            name="vector",
            _root_kind="vector",
            attributes={
                "element_type": Struct(
                    kind="type",
                    name="mlody-lineage-event",
                    _root_kind="record",
                    attributes={},
                    _allowed_attrs={},
                )
            },
            _allowed_attrs={},
        )
        value_struct = Struct(
            kind="value",
            name="employees_local",
            type=Struct(
                kind="type",
                name="mlody-value",
                virtual_attributes=[Struct(name="lineage", type=lineage_type)],
                _allowed_attrs={},
            ),
            location=Struct(kind="location", type="posix", path=str(destination_path)),
            source=":employees",
            _source_value=Struct(
                kind="value",
                name="employees",
                location=Struct(
                    kind="location",
                    type="remote",
                    attributes={"uri": "https://example.com/employees.csv"},
                ),
                representation=Struct(
                    kind="representation",
                    name="csv",
                    separator=",",
                    header_required=True,
                    multifile=False,
                    attributes={
                        "separator": ",",
                        "header_required": True,
                        "multifile": False,
                    },
                ),
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        )
        monkeypatch.setattr(
            "mlody.core.lineage._default_db_path",
            lambda: tmp_path / "mlody.sqlite",
        )
        remember_runtime_label(value_struct, "@demo//datasets:employees_local")
        remember_runtime_label(value_struct._source_value, "@demo//datasets:employees")

        lineage_attr = lookup_runtime_attribute(value_struct, "lineage")

        assert lineage_attr is not None
        with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
            mock_materialize.return_value = _remote_asset(
                staged_path,
                uri="https://example.com/employees.csv",
                content_hash=content_hash,
            )

            lineage = lineage_attr.materializer(value_struct)

        assert destination_path.read_text() == staged_path.read_text()
        assert [event.source for event in lineage] == [
            "downloaded from",
            "copied from",
        ]
        assert lineage[0].new_value.data == "https://example.com/employees.csv"
        assert lineage[1].details["destination_path"] == str(destination_path)
