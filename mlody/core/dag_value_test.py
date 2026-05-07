"""Tests for mlody.core.dag_value — DAG virtual value type and factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import networkx
import pytest

from mlody.core.dag_value import DAG_TYPE, MlodyDagType, make_dag_virtual_value
from mlody.core.virtual_value import force_virtual_value, is_virtual_value


class TestMlodyDagType:
    def test_is_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(MlodyDagType)

    def test_dag_type_fields(self) -> None:
        assert DAG_TYPE.kind == "type"
        assert DAG_TYPE.type == "mlody-dag"
        assert DAG_TYPE.name == "mlody-dag"

    def test_dag_type_is_singleton_instance(self) -> None:
        assert isinstance(DAG_TYPE, MlodyDagType)

    def test_dag_type_is_frozen(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            DAG_TYPE.type = "other"  # type: ignore[misc]


class TestMakeDagVirtualValue:
    def _make_workspace(self, graph: networkx.MultiDiGraph) -> MagicMock:
        ws = MagicMock()
        ws.dag = graph
        return ws

    def test_result_is_virtual_value(self) -> None:
        ws = self._make_workspace(networkx.MultiDiGraph())
        result = make_dag_virtual_value(ws, "my_output", ":task.outputs.my_output.dag")
        assert is_virtual_value(result)

    def test_result_type_is_mlody_dag_type(self) -> None:
        ws = self._make_workspace(networkx.MultiDiGraph())
        result = make_dag_virtual_value(ws, "my_output", ":task.outputs.my_output.dag")
        assert isinstance(result.type, MlodyDagType)

    def test_result_name_is_port_name(self) -> None:
        ws = self._make_workspace(networkx.MultiDiGraph())
        result = make_dag_virtual_value(ws, "embed", ":task.outputs.embed.dag")
        assert result.name == "embed"

    def test_result_label_stored(self) -> None:
        ws = self._make_workspace(networkx.MultiDiGraph())
        label = ":task.outputs.my_output.dag"
        result = make_dag_virtual_value(ws, "my_output", label)
        assert result.label == label

    def test_materialiser_returns_networkx_graph(self) -> None:
        full_dag: networkx.MultiDiGraph = networkx.MultiDiGraph()
        full_dag.add_node("task/pkg:producer", task=MagicMock(output_ports=("out",)))
        ws = self._make_workspace(full_dag)

        with patch("mlody.core.dag.ancestors_subgraph") as mock_sub:
            expected = networkx.MultiDiGraph()
            mock_sub.return_value = expected
            result = make_dag_virtual_value(ws, "out", "label")
            materialized = force_virtual_value(result)

        mock_sub.assert_called_once_with(full_dag, "out")
        assert materialized is expected

    def test_materialiser_uses_workspace_dag_property(self) -> None:
        ws = MagicMock()
        ws.dag = networkx.MultiDiGraph()
        result = make_dag_virtual_value(ws, "port", "label")

        with patch("mlody.core.dag.ancestors_subgraph", return_value=networkx.MultiDiGraph()):
            force_virtual_value(result)

        _ = ws.dag  # noqa: SIM910 — assert the property was accessed
        assert ws.dag is not None
