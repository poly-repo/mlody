"""Tests for mlody.core.action_graph selector behavior."""

from __future__ import annotations

from types import SimpleNamespace

import networkx

from mlody.core.action_graph import selection_for_label
from mlody.core.dag import Edge, TaskNode


def _make_port(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, type=SimpleNamespace(name="integer"))


def _make_action(name: str, *, outputs: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        inputs={},
        outputs={port: _make_port(port) for port in (outputs or [])},
        config={},
    )


def _make_task(name: str, action_name: str, *, outputs: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        kind="task",
        name=name,
        action=_make_action(action_name, outputs=outputs),
        inputs={},
        outputs={port: _make_port(port) for port in (outputs or [])},
        config={},
    )


def _make_workspace_with_output_name_collision() -> SimpleNamespace:
    dag = networkx.MultiDiGraph()

    upstream = _make_task("upstream", "train_action", outputs=["weights"])
    downstream = _make_task("downstream", "export_action", outputs=["report"])
    shadow = _make_task("shadow", "report_action", outputs=["report"])

    dag.add_node(
        "task/test:upstream",
        task=TaskNode(
            node_id="task/test:upstream",
            name="upstream",
            task=upstream,  # type: ignore[arg-type]
        ),
    )
    dag.add_node(
        "task/test:downstream",
        task=TaskNode(
            node_id="task/test:downstream",
            name="downstream",
            task=downstream,  # type: ignore[arg-type]
        ),
    )
    dag.add_node(
        "task/other:shadow",
        task=TaskNode(
            node_id="task/other:shadow",
            name="shadow",
            task=shadow,  # type: ignore[arg-type]
        ),
    )
    dag.add_edge(
        "task/test:upstream",
        "task/test:downstream",
        edge=Edge(src_port="weights", dst_path="weights"),
    )

    return SimpleNamespace(
        dag=dag,
        root_infos=None,
        _workspace_root=None,
        _monorepo_root=None,
    )


def test_selection_for_label_uses_task_qualified_output() -> None:
    workspace = _make_workspace_with_output_name_collision()

    selection = selection_for_label(
        workspace,
        "//test:downstream.outputs.report",
    )

    assert selection.kind == "task-output"
    assert set(selection.graph.nodes) == {
        "task/test:upstream",
        "task/test:downstream",
    }
    assert "task/other:shadow" not in selection.graph.nodes
