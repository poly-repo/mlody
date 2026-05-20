"""Action-graph planning helpers shared by ``show`` and virtual values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import networkx

from mlody.core.dag import (
    TaskNode,
    ValueNode,
    ancestors_subgraph,
    task_output_ancestors_subgraph,
)
from mlody.core.targets import TargetAddress, parse_target
from mlody.core.workspace import Workspace


@dataclass(frozen=True)
class ActionGraphSelection:
    """Structural DAG slice relevant to an action-graph request."""

    requested_label: str
    kind: str
    graph: networkx.MultiDiGraph
    target_node_id: str | None = None


@dataclass(frozen=True)
class MlodyActionGraphNode:
    """One executable node in the internal action graph."""

    node_id: str
    executor: str
    operation: str
    title: str
    detail: str | None = None
    description: str | None = None
    executor_detail: str | None = None
    structural_node_id: str | None = None


def _workspace_relative_stem(workspace: Workspace) -> str:
    workspace_root = getattr(workspace, "_workspace_root", None)
    monorepo_root = getattr(workspace, "_monorepo_root", None)
    if not isinstance(workspace_root, Path) or not isinstance(monorepo_root, Path):
        return ""
    if workspace_root == monorepo_root:
        return ""
    try:
        return str(workspace_root.relative_to(monorepo_root))
    except ValueError:
        return ""


def _registry_stem_for_address(workspace: Workspace, address: TargetAddress) -> str:
    stem_parts: list[str] = []
    root_infos = getattr(workspace, "root_infos", None)
    if address.root is not None:
        root_info = (
            root_infos.get(address.root) if isinstance(root_infos, Mapping) else None
        )
        if root_info is not None:
            root_path_value = getattr(root_info, "path", None)
            root_path = (
                root_path_value.lstrip("/").rstrip("/")
                if isinstance(root_path_value, str)
                else ""
            )
            if root_path:
                stem_parts.append(root_path)
    else:
        workspace_rel = _workspace_relative_stem(workspace)
        if workspace_rel:
            stem_parts.append(workspace_rel)
    if address.package_path:
        stem_parts.append(address.package_path.lstrip("/").rstrip("/"))
    return "/".join(part for part in stem_parts if part)


def task_node_id_for_address(workspace: Workspace, address: TargetAddress) -> str:
    stem = _registry_stem_for_address(workspace, address)
    return f"task/{stem}:{address.target_name}" if stem else f"task/:{address.target_name}"


def _value_node_id_for_address(workspace: Workspace, address: TargetAddress) -> str:
    stem = _registry_stem_for_address(workspace, address)
    return (
        f"value/{stem}:{address.target_name}" if stem else f"value/:{address.target_name}"
    )


def _structural_subgraph(
    dag: networkx.MultiDiGraph,
    *,
    node_id: str,
) -> networkx.MultiDiGraph:
    relevant_nodes = networkx.ancestors(dag, node_id) | {node_id}
    return dag.subgraph(relevant_nodes).copy()


def selection_for_label(workspace: Workspace, label: str) -> ActionGraphSelection:
    """Select the structural DAG slice relevant to one concrete label."""
    try:
        address = parse_target(label)
    except ValueError:
        return ActionGraphSelection(
            requested_label=label,
            kind="none",
            graph=networkx.MultiDiGraph(),
        )

    dag = getattr(workspace, "dag", None)
    if not isinstance(dag, networkx.MultiDiGraph):
        return ActionGraphSelection(
            requested_label=label,
            kind="none",
            graph=networkx.MultiDiGraph(),
        )
    if len(address.field_path) >= 2 and address.field_path[0] == "outputs":
        task_node_id = task_node_id_for_address(workspace, address)
        return ActionGraphSelection(
            requested_label=label,
            kind="task-output",
            graph=task_output_ancestors_subgraph(
                dag,
                task_node_id,
                address.field_path[1],
            ),
        )

    task_node_id = task_node_id_for_address(workspace, address)
    if task_node_id in dag.nodes:
        return ActionGraphSelection(
            requested_label=label,
            kind="task-node",
            graph=_structural_subgraph(dag, node_id=task_node_id),
            target_node_id=task_node_id,
        )

    value_node_id = _value_node_id_for_address(workspace, address)
    if value_node_id in dag.nodes:
        return ActionGraphSelection(
            requested_label=label,
            kind="value-node",
            graph=_structural_subgraph(dag, node_id=value_node_id),
            target_node_id=value_node_id,
        )

    return ActionGraphSelection(
        requested_label=label,
        kind="none",
        graph=networkx.MultiDiGraph(),
    )


def selection_for_port(
    workspace: Workspace,
    port_name: str,
    *,
    requested_label: str,
) -> ActionGraphSelection:
    """Select the action-graph structural slice for a source/output value port."""
    dag = getattr(workspace, "dag", None)
    if not isinstance(dag, networkx.MultiDiGraph) or not port_name:
        return ActionGraphSelection(
            requested_label=requested_label,
            kind="none",
            graph=networkx.MultiDiGraph(),
        )
    return ActionGraphSelection(
        requested_label=requested_label,
        kind="dag-target",
        graph=ancestors_subgraph(dag, port_name),
    )


def build_action_graph(selection: ActionGraphSelection) -> networkx.DiGraph:
    """Lower a structural DAG slice into executable ``mlody`` action nodes."""
    action_graph = networkx.DiGraph()
    structural_node_ids: list[str] = []
    mlody_executor_detail = (
        "Runs in-process Python in the current mlody CLI/server runtime."
    )

    for structural_node_id, data in selection.graph.nodes(data=True):
        if "task" in data:
            task_node = data["task"]
            assert isinstance(task_node, TaskNode)
            action = MlodyActionGraphNode(
                node_id=f"struct:{structural_node_id}",
                executor="mlody",
                operation="structural-task",
                title="Task Context",
                detail=task_node.name or structural_node_id,
                description=(
                    "Loads the task node selected from the pruned task/value graph."
                ),
                executor_detail=mlody_executor_detail,
                structural_node_id=structural_node_id,
            )
        elif "value" in data:
            value_node = data["value"]
            assert isinstance(value_node, ValueNode)
            action = MlodyActionGraphNode(
                node_id=f"struct:{structural_node_id}",
                executor="mlody",
                operation="structural-value",
                title="Value Context",
                detail=value_node.name or structural_node_id,
                description=(
                    "Loads the value node selected from the pruned task/value graph."
                ),
                executor_detail=mlody_executor_detail,
                structural_node_id=structural_node_id,
            )
        else:
            continue
        structural_node_ids.append(action.node_id)
        action_graph.add_node(action.node_id, action=action)

    for src_id, dst_id in selection.graph.edges():
        src_action_id = f"struct:{src_id}"
        dst_action_id = f"struct:{dst_id}"
        if src_action_id in action_graph.nodes and dst_action_id in action_graph.nodes:
            action_graph.add_edge(src_action_id, dst_action_id)

    prepare_node_id = f"prepare:{selection.requested_label}"
    action_graph.add_node(
        prepare_node_id,
        action=MlodyActionGraphNode(
            node_id=prepare_node_id,
            executor="mlody",
            operation="prepare-show-value",
            title="Prepare Display",
            detail=selection.requested_label,
            description=(
                "Consumes the already-resolved requested value and runs show-time "
                "preparation: force virtual values, derive the display payload, "
                "and build a tabular preview when applicable."
            ),
            executor_detail=mlody_executor_detail,
        ),
    )

    if structural_node_ids:
        sink_ids = [
            node_id
            for node_id in structural_node_ids
            if action_graph.out_degree(node_id) == 0
        ]
        for sink_id in sink_ids:
            action_graph.add_edge(sink_id, prepare_node_id)

    action_graph.graph["prepare_node_id"] = prepare_node_id
    action_graph.graph["requested_label"] = selection.requested_label
    return action_graph
