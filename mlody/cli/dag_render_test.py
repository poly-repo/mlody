"""Unit tests for shared DAG selection and rendering helpers."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import networkx
from rich.console import Console

from mlody.cli.dag_render import (
    build_dag_table,
    format_action_cell,
    resolve_dag_selection,
    resolve_show_output_selection,
    short_type_name,
)
from mlody.core.dag import Edge, TaskNode


def _make_type(
    name: str,
    *,
    root_kind: str | None = None,
    attributes: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        type=name,
        _root_kind=root_kind or name,
        attributes=attributes or {},
    )


def _make_port(name: str, type_obj: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, type=type_obj or _make_type("integer"))


def _make_action(
    name: str,
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    config: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        inputs=[_make_port(port) for port in (inputs or [])],
        outputs=[_make_port(port) for port in (outputs or [])],
        config=[_make_port(port) for port in (config or [])],
    )


def _make_task_struct(
    name: str,
    action_name: str,
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    config: list[str] | None = None,
    action_inputs: list[str] | None = None,
    action_outputs: list[str] | None = None,
    action_config: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        kind="task",
        name=name,
        action=_make_action(
            action_name,
            inputs=action_inputs,
            outputs=action_outputs,
            config=action_config,
        ),
        inputs=[_make_port(port) for port in (inputs or [])],
        outputs=[_make_port(port) for port in (outputs or [])],
        config=[_make_port(port) for port in (config or [])],
    )


def _make_graph() -> networkx.MultiDiGraph:
    dag = networkx.MultiDiGraph()

    upstream_struct = _make_task_struct(
        "upstream",
        "train_action",
        outputs=["weights"],
        action_outputs=["raw_weights"],
    )
    downstream_struct = _make_task_struct(
        "downstream",
        "export_action",
        inputs=["weights"],
        outputs=["model_checkpoint"],
        config=["epochs"],
        action_inputs=["weights"],
        action_outputs=["model_checkpoint"],
        action_config=["epochs"],
    )

    dag.add_node(
        "task/test:upstream",
        task=TaskNode(
            node_id="task/test:upstream",
            name="upstream",
            task=upstream_struct,  # type: ignore[arg-type]
        ),
    )
    dag.add_node(
        "task/test:downstream",
        task=TaskNode(
            node_id="task/test:downstream",
            name="downstream",
            task=downstream_struct,  # type: ignore[arg-type]
        ),
    )
    dag.add_edge(
        "task/test:upstream",
        "task/test:downstream",
        edge=Edge(src_port="weights", dst_path="weights"),
    )
    return dag


def test_short_type_name_prefers_nested_type_name() -> None:
    value = SimpleNamespace(type=SimpleNamespace(name="dataset"))

    assert short_type_name(value) == "dataset"


def test_short_type_name_renders_aggregate_alias_with_detail() -> None:
    value = SimpleNamespace(
        type=_make_type(
            "dataset",
            root_kind="vector",
            attributes={"element_type": _make_type("row", root_kind="record")},
        )
    )

    assert short_type_name(value) == "dataset (vector[row])"


def test_format_action_cell_renders_short_port_summaries() -> None:
    action = _make_action(
        "train_action",
        inputs=["dataset"],
        outputs=["model"],
        config=["epochs"],
    )

    rendered = format_action_cell(action, "fallback")

    assert "train_action" in rendered
    assert "AIn:  dataset:integer" in rendered
    assert "AOut: model:integer" in rendered
    assert "ACfg: epochs:integer" in rendered


def test_format_action_cell_renders_aggregate_type_labels() -> None:
    point_type = _make_type(
        "point",
        root_kind="tuple",
        attributes={"_element_types": [_make_type("float"), _make_type("float")]},
    )
    action = SimpleNamespace(
        name="train_action",
        inputs=[
            _make_port(
                "dataset",
                _make_type(
                    "dataset",
                    root_kind="vector",
                    attributes={"element_type": _make_type("row", root_kind="record")},
                ),
            )
        ],
        outputs=[],
        config=[_make_port("point", point_type)],
    )

    rendered = format_action_cell(action, "fallback")

    assert "dataset:dataset (vector[row])" in rendered
    assert "point:point (tuple[float, float])" in rendered


def test_build_dag_table_renders_dependency_rows() -> None:
    dag = _make_graph()
    table = build_dag_table(dag, "Workspace DAG")

    buffer = StringIO()
    console = Console(file=buffer, record=True, width=140)
    console.print(table)
    rendered = console.export_text()

    assert "Workspace DAG" in rendered
    assert "task/test:upstream" in rendered
    assert "task/test:downstream" in rendered
    assert "weights → weights" in rendered
    assert "Cfg: epochs:integer" in rendered


def test_build_dag_table_renders_aggregate_type_labels() -> None:
    dag = networkx.MultiDiGraph()
    dataset_type = _make_type(
        "dataset",
        root_kind="vector",
        attributes={"element_type": _make_type("row", root_kind="record")},
    )
    point_type = _make_type(
        "point",
        root_kind="tuple",
        attributes={"_element_types": [_make_type("float"), _make_type("float")]},
    )
    task_struct = SimpleNamespace(
        kind="task",
        name="downstream",
        action=SimpleNamespace(name="export_action", inputs=[], outputs=[], config=[]),
        inputs=[_make_port("dataset", dataset_type)],
        outputs=[],
        config=[_make_port("point", point_type)],
    )
    dag.add_node(
        "task/test:downstream",
        task=TaskNode(
            node_id="task/test:downstream",
            name="downstream",
            task=task_struct,  # type: ignore[arg-type]
        ),
    )

    table = build_dag_table(dag, "Workspace DAG")
    buffer = StringIO()
    console = Console(file=buffer, record=True, width=140)
    console.print(table)
    rendered = console.export_text()

    assert "dataset:dataset (vector[row])" in rendered
    assert "point:point (tuple[float, float])" in rendered


def test_resolve_dag_selection_accepts_task_and_output_labels() -> None:
    dag = _make_graph()

    task_selection = resolve_dag_selection(dag, "//test:downstream")
    output_selection = resolve_dag_selection(
        dag,
        "//test:downstream.outputs.model_checkpoint",
    )

    assert task_selection.resolved_label == "task/test:downstream"
    assert set(task_selection.graph.nodes) == {
        "task/test:upstream",
        "task/test:downstream",
    }
    assert output_selection.resolved_label == "model_checkpoint"
    assert set(output_selection.graph.nodes) == {
        "task/test:upstream",
        "task/test:downstream",
    }


def test_resolve_dag_selection_suggests_missing_outputs_prefix() -> None:
    dag = _make_graph()

    selection = resolve_dag_selection(dag, "//test:downstream.model_checkpoint")

    assert len(selection.graph.nodes) == 0
    assert selection.suggestion_text == (
        "Did you mean: '//test:downstream.outputs.model_checkpoint'?"
    )


def test_resolve_show_output_selection_only_accepts_output_labels() -> None:
    dag = _make_graph()

    output_selection = resolve_show_output_selection(
        dag,
        "//test:downstream.outputs.model_checkpoint",
    )
    task_selection = resolve_show_output_selection(dag, "//test:downstream")

    assert output_selection is not None
    assert set(output_selection.graph.nodes) == {
        "task/test:upstream",
        "task/test:downstream",
    }
    assert task_selection is None
