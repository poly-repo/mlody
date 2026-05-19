"""Shared action-graph rendering helpers for CLI and stage."""

from __future__ import annotations

from typing import cast

import networkx
from rich.table import Table
from rich.text import Text

from mlody.core.action_graph import MlodyActionGraphNode

_STAGE_ACTION_LAYER_SEP = 320.0
_STAGE_ACTION_NODE_SEP = 180.0
_STAGE_ACTION_PADDING = 96.0


def build_action_graph_table(action_graph: networkx.DiGraph, title: str) -> Table:
    """Build the Rich table used by action-graph displays in ``show``."""
    order = list(networkx.topological_sort(action_graph))

    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Step", style="cyan", no_wrap=True, ratio=3)
    table.add_column("Executor", style="magenta", no_wrap=True, ratio=2)
    table.add_column("Operation", style="yellow", no_wrap=True, ratio=3)
    table.add_column("Executes", style="white", ratio=5)
    table.add_column("Dependencies", style="white", ratio=4)

    for node_id in order:
        action = cast(MlodyActionGraphNode, action_graph.nodes[node_id]["action"])
        dependencies = [
            _dependency_text(cast(MlodyActionGraphNode, action_graph.nodes[src_id]["action"]))
            for src_id, _ in action_graph.in_edges(node_id)
        ]
        table.add_row(
            Text(action.title),
            Text(action.executor),
            Text(action.operation),
            Text(_detail_text(action)),
            Text("\n".join(dependencies) if dependencies else "—"),
        )

    return table


def build_stage_action_graph_data(action_graph: networkx.DiGraph) -> dict[str, object]:
    """Serialize an action graph into a stage-friendly payload."""
    positions = _stage_action_positions(action_graph)
    nodes: list[dict[str, object]] = []

    for node_id in networkx.topological_sort(action_graph):
        action = cast(MlodyActionGraphNode, action_graph.nodes[node_id]["action"])
        position = positions[node_id]
        nodes.append(
            {
                "id": node_id,
                "kind": _stage_action_kind(getattr(action, "operation", "action")),
                "title": getattr(action, "title", "Action"),
                "subtitle": getattr(action, "detail", None),
                "description": getattr(action, "description", None),
                "executor": getattr(action, "executor", "mlody"),
                "executorDetail": getattr(action, "executor_detail", None),
                "operation": getattr(action, "operation", "action"),
                "structuralNodeId": getattr(action, "structural_node_id", None),
                "position": {
                    "x": position[0],
                    "y": position[1],
                },
            }
        )

    edges: list[dict[str, object]] = []
    for index, (src_id, dst_id) in enumerate(action_graph.edges()):
        edges.append(
            {
                "id": f"edge-{index}",
                "sourceNodeId": src_id,
                "targetNodeId": dst_id,
            }
        )

    return {
        "nodes": nodes,
        "edges": edges,
    }


def _dependency_text(action: MlodyActionGraphNode) -> str:
    detail = getattr(action, "detail", None)
    title = getattr(action, "title", "Step")
    if detail:
        return f"{title}: {detail}"
    return title


def _detail_text(action: MlodyActionGraphNode) -> str:
    parts: list[str] = []
    detail = getattr(action, "detail", None)
    description = getattr(action, "description", None)
    executor_detail = getattr(action, "executor_detail", None)
    structural_node_id = getattr(action, "structural_node_id", None)
    if detail:
        parts.append(detail)
    if description:
        parts.append(description)
    if executor_detail:
        parts.append(executor_detail)
    if structural_node_id:
        parts.append(f"structural node: {structural_node_id}")
    return "\n".join(parts) if parts else "—"


def _stage_action_kind(operation: str) -> str:
    if operation == "structural-task":
        return "task"
    if operation == "structural-value":
        return "value"
    if operation == "resolve-label":
        return "resolve"
    if operation == "prepare-show-value":
        return "prepare"
    return "action"


def _stage_action_positions(
    action_graph: networkx.DiGraph,
) -> dict[str, tuple[float, float]]:
    if not action_graph.nodes:
        return {}

    generations = list(networkx.topological_generations(action_graph))
    if not generations:
        generations = [list(networkx.topological_sort(action_graph))]

    positions: dict[str, tuple[float, float]] = {}
    for layer_index, layer in enumerate(generations):
        ordered_layer = list(layer)
        for node_index, node_id in enumerate(ordered_layer):
            positions[node_id] = (
                _STAGE_ACTION_PADDING + layer_index * _STAGE_ACTION_LAYER_SEP,
                _STAGE_ACTION_PADDING + node_index * _STAGE_ACTION_NODE_SEP,
            )
    return positions
