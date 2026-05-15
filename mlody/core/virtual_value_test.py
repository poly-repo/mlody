"""Regression tests for mlody.core.virtual_value traversal helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.virtual_value import lookup_runtime_attribute, step_object


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
    def test_lineage_attribute_populates_remote_download_lineage(self) -> None:
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
            _lineage=[],
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

        lineage_attr = lookup_runtime_attribute(value_struct, "lineage")

        assert lineage_attr is not None
        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = Struct(
                uri="https://example.com/data.csv",
                path=Path("/tmp/staged.csv"),
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
    ) -> None:
        staged_path = tmp_path / "staged.csv"
        staged_path.write_text("name,salary\nAlice,120000\n")
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
            _lineage=[],
            location=Struct(kind="location", type="posix", path=str(destination_path)),
            source=":employees",
            _source_value=Struct(
                kind="value",
                name="employees",
                _lineage=[],
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

        lineage_attr = lookup_runtime_attribute(value_struct, "lineage")

        assert lineage_attr is not None
        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = Struct(
                uri="https://example.com/employees.csv",
                path=staged_path,
                content_hash="abc123",
            )

            lineage = lineage_attr.materializer(value_struct)

        assert destination_path.read_text() == staged_path.read_text()
        assert [event.source for event in lineage] == [
            "downloaded from",
            "copied from",
        ]
        assert lineage[0].new_value.data == "https://example.com/employees.csv"
        assert lineage[1].details["destination_path"] == str(destination_path)
        assert len(value_struct._source_value._lineage) == 1
        assert value_struct._source_value._lineage[0].source == "downloaded from"
