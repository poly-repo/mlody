"""Tests for the shared show action-graph executor."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import networkx
import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.cli.show_execution import (
    CliPreparedShowResult,
    PreparedShowValue,
    ShowActionExecutionContext,
    ShowActionStubResult,
    execute_show_action_graph,
    make_cli_prepare_display,
)
from mlody.core.action_graph import ActionGraphSelection, MlodyActionGraphNode
from mlody.resolver.values.registry_backed import MlodyValueValue


def _make_selection() -> ActionGraphSelection:
    return ActionGraphSelection(
        requested_label="//test:report.outputs.value",
        kind="task-output",
        graph=networkx.MultiDiGraph(),
    )


def test_execute_show_action_graph_runs_nodes_topologically(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    selection = _make_selection()
    action_graph = networkx.DiGraph()
    action_graph.add_node(
        "payload:before:test:0",
        action=MlodyActionGraphNode(
            node_id="payload:before:test:0",
            executor="mlody",
            operation="payload-before",
            title="Before Action",
        ),
    )
    action_graph.add_node(
        "struct:test:task",
        action=MlodyActionGraphNode(
            node_id="struct:test:task",
            executor="mlody",
            operation="structural-task",
            title="Task Context",
        ),
    )
    action_graph.add_node(
        "prepare://test:report.outputs.value",
        action=MlodyActionGraphNode(
            node_id="prepare://test:report.outputs.value",
            executor="mlody",
            operation="prepare-show-value",
            title="Prepare Display",
        ),
    )
    action_graph.add_edge("payload:before:test:0", "struct:test:task")
    action_graph.add_edge("struct:test:task", "prepare://test:report.outputs.value")
    action_graph.graph["prepare_node_id"] = "prepare://test:report.outputs.value"

    monkeypatch.setattr(
        "mlody.cli.show_execution.selection_for_label",
        lambda _workspace, _label: selection,
    )
    monkeypatch.setattr(
        "mlody.cli.show_execution.build_action_graph",
        lambda _selection: action_graph,
    )

    resolved_value = MlodyValueValue(struct=Struct(kind="value", name="report"))
    prepare_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    def _prepare_display(context: ShowActionExecutionContext) -> str:
        prepare_calls.append(
            (
                context.action.node_id,
                tuple(context.dependency_results),
                tuple(context.node_results),
            )
        )
        return "prepared-result"

    caplog.set_level(logging.INFO)
    execution = execute_show_action_graph(
        SimpleNamespace(),
        "//test:report.outputs.value",
        "label",
        resolve_label=lambda _label, _workspace: resolved_value,
        prepare_display=_prepare_display,
    )

    assert execution.final_result == "prepared-result"
    assert list(execution.node_results) == [
        "payload:before:test:0",
        "struct:test:task",
        "prepare://test:report.outputs.value",
    ]
    assert execution.node_results["payload:before:test:0"] == ShowActionStubResult(
        node_id="payload:before:test:0",
        operation="payload-before",
        title="Before Action",
        detail=None,
    )
    assert execution.node_results["struct:test:task"] == ShowActionStubResult(
        node_id="struct:test:task",
        operation="structural-task",
        title="Task Context",
        detail=None,
    )
    assert prepare_calls == [
        (
            "prepare://test:report.outputs.value",
            ("struct:test:task",),
            ("payload:before:test:0", "struct:test:task"),
        )
    ]
    assert callable(action_graph.nodes["payload:before:test:0"]["action"].callable)
    assert callable(action_graph.nodes["struct:test:task"]["action"].callable)
    assert (
        action_graph.nodes["prepare://test:report.outputs.value"]["action"].callable
        is _prepare_display
    )
    assert [record.message for record in caplog.records if "would run" in record.message] == [
        "Show action payload:before:test:0 (payload-before) would run once implemented",
        "Show action struct:test:task (structural-task) would run once implemented",
    ]


def test_make_cli_prepare_display_returns_prepared_cli_result() -> None:
    resolved_value = MlodyValueValue(struct=Struct(kind="value", name="report"))
    prepare_display = make_cli_prepare_display(
        display_value=lambda _value: {"kind": "payload"}
    )

    result = prepare_display(
        ShowActionExecutionContext(
            workspace=SimpleNamespace(),
            selection=_make_selection(),
            action_graph=networkx.DiGraph(),
            requested_label="//test:report.outputs.value",
            concrete_label="label",
            resolved_value=resolved_value,
            action=MlodyActionGraphNode(
                node_id="prepare://test:report.outputs.value",
                executor="mlody",
                operation="prepare-show-value",
                title="Prepare Display",
            ),
            dependency_results={},
            node_results={},
        )
    )

    assert isinstance(result, CliPreparedShowResult)
    assert result.value is resolved_value
    assert result.prepared == PreparedShowValue(
        value=resolved_value,
        display_payload={"kind": "payload"},
    )
