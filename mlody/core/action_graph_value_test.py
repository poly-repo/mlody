"""Tests for mlody.core.action_graph_value — action-graph virtual values."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import networkx
import pytest

from mlody.core.action_graph import ActionGraphSelection
from mlody.core.action_graph_value import (
    ACTION_GRAPH_TYPE,
    MlodyActionGraphType,
    make_action_graph_virtual_value,
)
from mlody.core.virtual_value import force_virtual_value, is_virtual_value


class TestMlodyActionGraphType:
    def test_is_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(MlodyActionGraphType)

    def test_type_fields(self) -> None:
        assert ACTION_GRAPH_TYPE.kind == "type"
        assert ACTION_GRAPH_TYPE.type == "mlody-action-graph"
        assert ACTION_GRAPH_TYPE.name == "mlody-action-graph"

    def test_type_is_singleton_instance(self) -> None:
        assert isinstance(ACTION_GRAPH_TYPE, MlodyActionGraphType)

    def test_type_is_frozen(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            ACTION_GRAPH_TYPE.type = "other"  # type: ignore[misc]


class TestMakeActionGraphVirtualValue:
    def _make_workspace(self) -> MagicMock:
        ws = MagicMock()
        ws.dag = networkx.MultiDiGraph()
        return ws

    def test_result_is_virtual_value(self) -> None:
        ws = self._make_workspace()
        result = make_action_graph_virtual_value(
            ws,
            "my_output",
            ":task.outputs.my_output.agraph",
        )
        assert is_virtual_value(result)

    def test_result_type_is_mlody_action_graph_type(self) -> None:
        ws = self._make_workspace()
        result = make_action_graph_virtual_value(
            ws,
            "my_output",
            ":task.outputs.my_output.agraph",
        )
        assert isinstance(result.type, MlodyActionGraphType)

    def test_result_name_is_port_name(self) -> None:
        ws = self._make_workspace()
        result = make_action_graph_virtual_value(ws, "embed", ":task.outputs.embed.agraph")
        assert result.name == "embed"

    def test_result_label_stored(self) -> None:
        ws = self._make_workspace()
        label = ":task.outputs.my_output.agraph"
        result = make_action_graph_virtual_value(ws, "my_output", label)
        assert result.label == label

    def test_materialiser_builds_action_graph_for_parent_label(self) -> None:
        ws = self._make_workspace()
        selection = ActionGraphSelection(
            requested_label=":task.outputs.my_output",
            kind="dag-target",
            graph=networkx.MultiDiGraph(),
        )
        expected = networkx.DiGraph()
        expected.add_node(
            "prepare:test",
            action=SimpleNamespace(
                node_id="prepare:test",
                executor="mlody",
                operation="prepare-show-value",
                title="Prepare Display",
            ),
        )

        with (
            patch(
                "mlody.core.action_graph.selection_for_port",
                return_value=selection,
            ) as mock_select,
            patch(
                "mlody.core.action_graph.build_action_graph",
                return_value=expected,
            ) as mock_build,
        ):
            result = make_action_graph_virtual_value(
                ws,
                "my_output",
                ":task.outputs.my_output.agraph",
            )
            materialized = force_virtual_value(result)

        mock_select.assert_called_once_with(
            ws,
            "my_output",
            requested_label=":task.outputs.my_output",
        )
        mock_build.assert_called_once_with(selection)
        assert materialized is expected
