"""Action-graph planning helpers shared by ``show`` and virtual values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import networkx

from mlody.core.dag import (
    Edge,
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
    payload: "MlodyActionGraphNodePayload" = field(
        default_factory=lambda: MlodyActionGraphNodePayload()
    )


@dataclass(frozen=True)
class MlodyActionGraphPayloadAction:
    """One future action attached to an action-graph node payload slot."""

    name: str
    description: str | None = None


@dataclass(frozen=True)
class MlodyActionGraphNodePayload:
    """Structured node payload for execution hooks around one action node."""

    before: tuple[MlodyActionGraphPayloadAction, ...] = ()
    after: tuple[MlodyActionGraphPayloadAction, ...] = ()
    around: tuple[MlodyActionGraphPayloadAction, ...] = ()


def _demo_task_node_payload() -> MlodyActionGraphNodePayload:
    """Return the temporary demo payload injected onto task-derived action nodes.

    Keep this helper isolated so the debug/demo hook can be removed cleanly when
    real before/after/around actions start driving the payload.
    """
    return MlodyActionGraphNodePayload(
        before=(
            MlodyActionGraphPayloadAction(
                name="demo-task-before",
                description="temporary debug/demo action",
            ),
        ),
        around=(
            MlodyActionGraphPayloadAction(
                name="demo-task-around-visualization-debug-action-with-extra-detail",
                description=(
                    "temporary debug/demo around action with a deliberately long "
                    "name for graph layout inspection"
                ),
            ),
        ),
        after=(
            MlodyActionGraphPayloadAction(
                name="demo-task-after-visualization-debug-action-with-extra-detail",
                description=(
                    "temporary debug/demo after action with a deliberately long "
                    "name for graph layout inspection"
                ),
            ),
        ),
    )


@dataclass(frozen=True)
class MlodyActionGraphDependency:
    """One explicit dependency edge between two action-graph nodes."""

    source_node_id: str
    target_node_id: str
    origin: str
    structural_edges: tuple[Edge, ...] = ()


def _add_action_dependency(
    action_graph: networkx.DiGraph,
    dependency: MlodyActionGraphDependency,
) -> None:
    action_graph.add_edge(
        dependency.source_node_id,
        dependency.target_node_id,
        dependency=dependency,
    )


def _payload_action_node_id(parent_node_id: str, phase: str, index: int) -> str:
    return f"payload:{phase}:{parent_node_id}:{index}"


def _payload_action_title(phase: str) -> str:
    return {
        "before": "Before Action",
        "around": "Around Action",
        "after": "After Action",
    }.get(phase, "Payload Action")


def _payload_phase_nodes(
    parent_action: MlodyActionGraphNode,
    *,
    phase: str,
) -> tuple[MlodyActionGraphNode, ...]:
    payload_actions = getattr(parent_action.payload, phase, ())
    if not isinstance(payload_actions, tuple):
        return ()
    return tuple(
        MlodyActionGraphNode(
            node_id=_payload_action_node_id(parent_action.node_id, phase, index),
            executor=parent_action.executor,
            operation=f"payload-{phase}",
            title=_payload_action_title(phase),
            detail=payload_action.name,
            description=payload_action.description,
            executor_detail=parent_action.executor_detail,
            structural_node_id=parent_action.structural_node_id,
        )
        for index, payload_action in enumerate(payload_actions)
    )


def _add_payload_chain(
    action_graph: networkx.DiGraph,
    node_ids: list[str],
) -> None:
    for src_node_id, dst_node_id in zip(node_ids, node_ids[1:]):
        _add_action_dependency(
            action_graph,
            MlodyActionGraphDependency(
                source_node_id=src_node_id,
                target_node_id=dst_node_id,
                origin="payload-chain",
            ),
        )


def _attach_payload_action_nodes(
    action_graph: networkx.DiGraph,
    action: MlodyActionGraphNode,
) -> str:
    before_nodes = _payload_phase_nodes(action, phase="before")
    around_nodes = _payload_phase_nodes(action, phase="around")
    after_nodes = _payload_phase_nodes(action, phase="after")

    for payload_node in (*before_nodes, *around_nodes, *after_nodes):
        action_graph.add_node(payload_node.node_id, action=payload_node)

    _add_payload_chain(
        action_graph,
        [*[node.node_id for node in before_nodes], action.node_id],
    )
    downstream_chain_ids = [
        action.node_id,
        *[node.node_id for node in around_nodes],
        *[node.node_id for node in after_nodes],
    ]
    _add_payload_chain(action_graph, downstream_chain_ids)
    return downstream_chain_ids[-1]


def _structural_action_dependencies(
    selection: ActionGraphSelection,
    *,
    action_node_ids: set[str],
) -> tuple[MlodyActionGraphDependency, ...]:
    seen: set[tuple[str, str]] = set()
    grouped_edges: dict[tuple[str, str], list[Edge]] = {}
    ordered_pairs: list[tuple[str, str]] = []

    for src_id, dst_id, data in selection.graph.edges(data=True):
        src_action_id = f"struct:{src_id}"
        dst_action_id = f"struct:{dst_id}"
        pair = (src_action_id, dst_action_id)
        if src_action_id not in action_node_ids or dst_action_id not in action_node_ids:
            continue
        if pair not in seen:
            seen.add(pair)
            ordered_pairs.append(pair)
            grouped_edges[pair] = []
        edge = data.get("edge")
        if isinstance(edge, Edge):
            grouped_edges[pair].append(edge)

    return tuple(
        MlodyActionGraphDependency(
            source_node_id=src_action_id,
            target_node_id=dst_action_id,
            origin="structural-dag",
            structural_edges=tuple(grouped_edges[(src_action_id, dst_action_id)]),
        )
        for src_action_id, dst_action_id in ordered_pairs
    )


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
    logical_exit_node_ids: dict[str, str] = {}
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
                payload=_demo_task_node_payload(),
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
        logical_exit_node_ids[action.node_id] = _attach_payload_action_nodes(
            action_graph, action
        )

    structural_dependencies = _structural_action_dependencies(
        selection,
        action_node_ids=set(structural_node_ids),
    )
    structural_source_node_ids: set[str] = set()
    for dependency in structural_dependencies:
        structural_source_node_ids.add(dependency.source_node_id)
        _add_action_dependency(
            action_graph,
            MlodyActionGraphDependency(
                source_node_id=dependency.source_node_id,
                target_node_id=dependency.target_node_id,
                origin=dependency.origin,
                structural_edges=dependency.structural_edges,
            ),
        )

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
            if node_id not in structural_source_node_ids
        ]
        for sink_id in sink_ids:
            _add_action_dependency(
                action_graph,
                MlodyActionGraphDependency(
                    source_node_id=logical_exit_node_ids[sink_id],
                    target_node_id=prepare_node_id,
                    origin="prepare-sink",
                ),
            )

    action_graph.graph["prepare_node_id"] = prepare_node_id
    action_graph.graph["requested_label"] = selection.requested_label
    return action_graph
