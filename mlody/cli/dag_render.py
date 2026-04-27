"""Shared DAG selection and rendering helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass

import networkx
from rich.console import Console
from rich.table import Table

from mlody.core.dag import Edge, TaskNode, ancestors_subgraph
from mlody.core.targets import TargetAddress, parse_target


@dataclass(frozen=True)
class DagSelectionResult:
    """Outcome of resolving a label against a task graph."""

    graph: networkx.MultiDiGraph
    resolved_label: str
    suggestion_text: str | None = None


def short_type_name(value: object) -> str:
    """Return a concise type label for a value-like object."""
    value_type = getattr(value, "type", None)
    if value_type is None:
        return "?"

    type_name = getattr(value_type, "name", None)
    if isinstance(type_name, str) and type_name:
        return type_name
    if isinstance(value_type, str) and value_type:
        return value_type
    return "?"


def format_value_list(values: object) -> str:
    """Format ports/config entries as ``name:type`` with short type names."""
    if not isinstance(values, list) or not values:
        return "—"

    rendered: list[str] = []
    for value in values:
        name = getattr(value, "name", None)
        if not isinstance(name, str) or not name:
            name = str(value)
        rendered.append(f"{name}:{short_type_name(value)}")
    return ", ".join(rendered)


def format_action_cell(action_obj: object, fallback_name: str) -> str:
    """Format action name plus AIn/AOut/ACfg summaries."""
    if action_obj is None:
        return fallback_name

    name = getattr(action_obj, "name", None)
    if not isinstance(name, str) or not name:
        name = fallback_name

    action_inputs = format_value_list(getattr(action_obj, "inputs", []))
    action_outputs = format_value_list(getattr(action_obj, "outputs", []))
    action_config = format_value_list(getattr(action_obj, "config", []))
    return (
        f"{name}\n"
        f"AIn:  {action_inputs}\n"
        f"AOut: {action_outputs}\n"
        f"ACfg: {action_config}"
    )


def build_dag_table(display_graph: networkx.MultiDiGraph, title: str) -> Table:
    """Build the Rich table used by ``mlody dag`` and DAG previews in ``show``."""
    order = list(networkx.topological_sort(display_graph))

    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Task", style="cyan", no_wrap=True, ratio=4)
    table.add_column("Action", style="magenta", no_wrap=False, ratio=2)
    table.add_column("Dependencies", style="white", ratio=5)

    for node_id in order:
        task_node = display_graph.nodes[node_id]["task"]
        task_struct = display_graph.nodes[node_id]["task_struct"]

        dependencies: list[str] = []
        for src_id, _, data in display_graph.in_edges(node_id, data=True):
            edge: Edge = data["edge"]
            dependencies.append(f"{src_id}\n  {edge.src_port} → {edge.dst_path}")

        inputs_str = format_value_list(getattr(task_struct, "inputs", []))
        outputs_str = format_value_list(getattr(task_struct, "outputs", []))
        config_str = format_value_list(getattr(task_struct, "config", []))
        task_cell = (
            f"{node_id}\n"
            f"In:  {inputs_str}\n"
            f"Out: {outputs_str}\n"
            f"Cfg: {config_str}"
        )
        table.add_row(
            task_cell,
            format_action_cell(getattr(task_struct, "action", None), task_node.action),
            "\n\n".join(dependencies) if dependencies else "—",
        )

    return table


def render_dag_table(
    display_graph: networkx.MultiDiGraph,
    title: str,
    *,
    console: Console,
) -> None:
    """Render the shared DAG table to the provided console."""
    console.print(build_dag_table(display_graph, title))


def resolve_dag_selection(
    dag: networkx.MultiDiGraph,
    label: str,
) -> DagSelectionResult:
    """Resolve a dag command label to the graph that should be displayed."""
    try:
        address = parse_target(label)
    except ValueError:
        return DagSelectionResult(
            graph=ancestors_subgraph(dag, label),
            resolved_label=label,
        )

    if len(address.field_path) == 2 and address.field_path[0] == "outputs":
        port_name = address.field_path[1]
        return DagSelectionResult(
            graph=ancestors_subgraph(dag, port_name),
            resolved_label=port_name,
        )

    if not address.field_path:
        node_id = _task_node_id_for_address(dag, address)
        if node_id is not None:
            relevant_nodes = networkx.ancestors(dag, node_id) | {node_id}
            return DagSelectionResult(
                graph=dag.subgraph(relevant_nodes).copy(),
                resolved_label=node_id,
            )

    return DagSelectionResult(
        graph=networkx.MultiDiGraph(),
        resolved_label=label,
        suggestion_text=_suggest_label_fix(dag, label),
    )


def resolve_show_output_selection(
    dag: networkx.MultiDiGraph,
    label: str,
) -> DagSelectionResult | None:
    """Resolve a show label to an output-only ancestor preview, if applicable."""
    try:
        address = parse_target(label)
    except ValueError:
        return None

    if len(address.field_path) != 2 or address.field_path[0] != "outputs":
        return None

    return DagSelectionResult(
        graph=ancestors_subgraph(dag, address.field_path[1]),
        resolved_label=label,
    )


def _task_node_id_for_address(
    dag: networkx.MultiDiGraph,
    address: TargetAddress,
) -> str | None:
    """Return the task node ID referenced by a parsed target address."""
    if address.package_path is not None:
        candidate = f"task/{address.package_path}:{address.target_name}"
        if candidate in dag.nodes:
            return candidate
        return None

    for node_id, data in dag.nodes(data=True):
        task_node: TaskNode = data["task"]
        if task_node.name == address.target_name:
            return node_id
    return None


def _suggest_label_fix(
    dag: networkx.MultiDiGraph,
    label: str,
) -> str | None:
    """Return a hint when a task-port label is missing ``inputs``/``outputs``."""
    try:
        address = parse_target(label)
    except ValueError:
        return None

    if len(address.field_path) != 1:
        return None

    node_id = _task_node_id_for_address(dag, address)
    if node_id is None:
        return None

    port_name = address.field_path[0]
    task_node: TaskNode = dag.nodes[node_id]["task"]
    base = _label_base(address)

    suggestions: list[str] = []
    if port_name in task_node.output_ports:
        suggestions.append(f"'{base}.outputs.{port_name}'")
    if port_name in task_node.input_ports:
        suggestions.append(f"'{base}.inputs.{port_name}'")

    if not suggestions:
        return None
    return "Did you mean: " + " or ".join(suggestions) + "?"


def _label_base(address: TargetAddress) -> str:
    """Return the canonical label prefix for a parsed task target."""
    if address.package_path is not None:
        root_prefix = f"@{address.root}//" if address.root else "//"
        return f"{root_prefix}{address.package_path}:{address.target_name}"
    return f":{address.target_name}"
