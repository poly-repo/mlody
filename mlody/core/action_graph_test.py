"""Tests for mlody.core.action_graph selector behavior."""

from __future__ import annotations

from types import SimpleNamespace

import networkx

from mlody.core.action_graph import (
    ActionGraphSelection,
    MlodyActionGraphDependency,
    build_action_graph,
    selection_for_label,
)
from mlody.core.dag import Edge, TaskNode, ValueNode


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


def _make_value(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        kind="value",
        name=name,
        type=SimpleNamespace(name="integer"),
        location=SimpleNamespace(type="inline"),
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


def test_build_action_graph_collapses_parallel_structural_edges() -> None:
    graph = networkx.MultiDiGraph()
    upstream = _make_task("upstream", "train_action", outputs=["weights", "cfg"])
    downstream = _make_task("downstream", "export_action", outputs=["report"])

    graph.add_node(
        "task/test:upstream",
        task=TaskNode(
            node_id="task/test:upstream",
            name="upstream",
            task=upstream,  # type: ignore[arg-type]
        ),
    )
    graph.add_node(
        "task/test:downstream",
        task=TaskNode(
            node_id="task/test:downstream",
            name="downstream",
            task=downstream,  # type: ignore[arg-type]
        ),
    )
    graph.add_edge(
        "task/test:upstream",
        "task/test:downstream",
        edge=Edge(src_port="weights", dst_path="artifact"),
    )
    graph.add_edge(
        "task/test:upstream",
        "task/test:downstream",
        edge=Edge(src_port="cfg", dst_path="cfg"),
    )

    action_graph = build_action_graph(
        ActionGraphSelection(
            requested_label="//test:downstream.outputs.report",
            kind="task-output",
            graph=graph,
        )
    )

    assert list(action_graph.in_edges("struct:task/test:downstream")) == [
        ("struct:task/test:upstream", "struct:task/test:downstream")
    ]
    dependency_data = action_graph.get_edge_data(
        "struct:task/test:upstream",
        "struct:task/test:downstream",
    )
    assert dependency_data is not None
    dependency = dependency_data["dependency"]
    assert isinstance(dependency, MlodyActionGraphDependency)
    assert dependency.origin == "structural-dag"
    assert dependency.structural_edges == (
        Edge(src_port="weights", dst_path="artifact"),
        Edge(src_port="cfg", dst_path="cfg"),
    )


def test_build_action_graph_preserves_task_output_to_config_value_chain() -> None:
    graph = networkx.MultiDiGraph()
    producer = _make_task("producer", "produce_action", outputs=["cfg_seed"])
    consumer = _make_task("consumer", "consume_action", outputs=["report"])
    config_value = _make_value("cfg_seed")

    graph.add_node(
        "task/test:producer",
        task=TaskNode(
            node_id="task/test:producer",
            name="producer",
            task=producer,  # type: ignore[arg-type]
        ),
    )
    graph.add_node(
        "value/test:cfg_seed",
        value=ValueNode(
            node_id="value/test:cfg_seed",
            name="cfg_seed",
            value=config_value,  # type: ignore[arg-type]
        ),
    )
    graph.add_node(
        "task/test:consumer",
        task=TaskNode(
            node_id="task/test:consumer",
            name="consumer",
            task=consumer,  # type: ignore[arg-type]
        ),
    )
    graph.add_edge(
        "task/test:producer",
        "value/test:cfg_seed",
        edge=Edge(src_port="cfg_seed", dst_path="cfg_seed"),
    )
    graph.add_edge(
        "value/test:cfg_seed",
        "task/test:consumer",
        edge=Edge(src_port="cfg_seed", dst_path="cfg_seed"),
    )

    action_graph = build_action_graph(
        ActionGraphSelection(
            requested_label="//test:consumer.outputs.report",
            kind="task-output",
            graph=graph,
        )
    )

    producer_dependency = action_graph.get_edge_data(
        "struct:task/test:producer",
        "struct:value/test:cfg_seed",
    )
    consumer_dependency = action_graph.get_edge_data(
        "struct:value/test:cfg_seed",
        "struct:task/test:consumer",
    )

    assert producer_dependency is not None
    assert consumer_dependency is not None
    assert producer_dependency["dependency"] == MlodyActionGraphDependency(
        source_node_id="struct:task/test:producer",
        target_node_id="struct:value/test:cfg_seed",
        origin="structural-dag",
        structural_edges=(Edge(src_port="cfg_seed", dst_path="cfg_seed"),),
    )
    assert consumer_dependency["dependency"] == MlodyActionGraphDependency(
        source_node_id="struct:value/test:cfg_seed",
        target_node_id="struct:task/test:consumer",
        origin="structural-dag",
        structural_edges=(Edge(src_port="cfg_seed", dst_path="cfg_seed"),),
    )
