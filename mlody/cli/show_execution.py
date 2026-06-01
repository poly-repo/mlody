"""Shared show planning and execution helpers for console and stage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import networkx
import pyarrow as pa

from mlody.core.action_graph import (
    ActionGraphSelection,
    MlodyActionGraphNode,
    build_action_graph,
    selection_for_label,
)
from mlody.core.dag import TaskNode, ValueNode
from mlody.core.derived import DerivedValueShapeError
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular.location_specs import source_from_value
from mlody.core.tabular.derived_source import DerivedSource
from mlody.core.workspace import Workspace, force
from mlody.resolver import (
    MlodyUnresolvedValue,
    MlodyValue,
    MlodyValueValue,
    MlodyVectorValue,
    resolve_label_to_value,
)


@dataclass(frozen=True)
class TabularPreviewFailure:
    """Failure raised while trying to preview a tabular value."""

    kind: str
    message: str
    fatal: bool = False


@dataclass(frozen=True)
class PreparedShowValue:
    """Prepared rendering inputs for a resolved ``show`` value."""

    value: MlodyValue
    children: tuple["PreparedShowValue", ...] = ()
    display_payload: object | None = None
    preview_table: pa.Table | None = None
    preview_total_rows: int | None = None
    preview_failure: TabularPreviewFailure | None = None
    source_failure: str | None = None


@dataclass(frozen=True)
class ShowActionGraphExecution:
    """Result of planning and executing a ``show`` action graph."""

    selection: ActionGraphSelection
    action_graph: networkx.DiGraph
    prepared_value: PreparedShowValue


def _preview_failure(
    exc: Exception,
    *,
    fatal: bool,
) -> TabularPreviewFailure:
    if isinstance(exc, DerivedValueShapeError):
        return TabularPreviewFailure(kind="derived-shape", message=str(exc), fatal=fatal)
    if isinstance(exc, MlodyQueryError):
        return TabularPreviewFailure(kind="query", message=str(exc), fatal=fatal)
    return TabularPreviewFailure(kind="preview", message=str(exc), fatal=fatal)


def _default_display_payload(value: MlodyValueValue) -> object:
    from mlody.core.dag_value import MlodyDagType  # noqa: PLC0415
    from mlody.core.virtual_value import force_virtual_value  # noqa: PLC0415

    if isinstance(getattr(value.struct, "type", None), MlodyDagType):
        return force_virtual_value(value.struct)
    return force(value.struct)


def prepare_show_value(
    value: MlodyValue,
    *,
    display_value: Callable[[MlodyValueValue], object] = _default_display_payload,
    db_conn: object | None = None,
) -> PreparedShowValue:
    if isinstance(value, MlodyVectorValue):
        return PreparedShowValue(
            value=value,
            children=tuple(
                prepare_show_value(child, display_value=display_value, db_conn=db_conn)
                for child in value.elements
            ),
        )

    if not isinstance(value, MlodyValueValue):
        return PreparedShowValue(value=value)

    display_payload = display_value(value)
    preview_table: pa.Table | None = None
    preview_total_rows: int | None = None
    preview_failure: TabularPreviewFailure | None = None
    source_failure: str | None = None

    if hasattr(display_payload, "as_mapping"):
        try:
            tabular_source = source_from_value(display_payload, db_conn=db_conn)
        except ValueError as exc:
            tabular_source = None
            source_failure = str(exc)
        if tabular_source is not None:
            try:
                preview = tabular_source.preview(50)
                preview_table = preview.table
                preview_total_rows = preview.total_rows
            except Exception as exc:  # noqa: BLE001
                preview_failure = _preview_failure(
                    exc,
                    fatal=isinstance(tabular_source, DerivedSource),
                )

    return PreparedShowValue(
        value=value,
        display_payload=display_payload,
        preview_table=preview_table,
        preview_total_rows=preview_total_rows,
        preview_failure=preview_failure,
        source_failure=source_failure,
    )


def execute_show_action_graph(
    workspace: Workspace,
    requested_label: str,
    concrete_label: object,
    *,
    resolve_label: Callable[[object, Workspace], MlodyValue] = resolve_label_to_value,
    display_value: Callable[[MlodyValueValue], object] = _default_display_payload,
    db_conn: object | None = None,
) -> ShowActionGraphExecution:
    resolved_value = resolve_label(concrete_label, workspace)
    selection = selection_for_label(workspace, requested_label)
    action_graph = build_action_graph(selection)
    results: dict[str, object] = {}

    for node_id in networkx.topological_sort(action_graph):
        action = cast(MlodyActionGraphNode, action_graph.nodes[node_id]["action"])
        if action.executor != "mlody":
            raise ValueError(f"Unsupported show executor: {action.executor!r}")
        if action.operation == "structural-task":
            assert action.structural_node_id is not None
            task_node = cast(
                TaskNode,
                selection.graph.nodes[action.structural_node_id]["task"],
            )
            results[node_id] = task_node.task
            continue
        if action.operation == "structural-value":
            assert action.structural_node_id is not None
            value_node = cast(
                ValueNode,
                selection.graph.nodes[action.structural_node_id]["value"],
            )
            results[node_id] = value_node.value
            continue
        if action.operation == "prepare-show-value":
            results[node_id] = prepare_show_value(
                resolved_value,
                display_value=display_value,
                db_conn=db_conn,
            )
            continue
        if action.operation.startswith("payload-"):
            results[node_id] = action
            continue
        raise ValueError(f"Unsupported show action operation: {action.operation!r}")

    prepare_node_id = cast(str, action_graph.graph["prepare_node_id"])
    return ShowActionGraphExecution(
        selection=selection,
        action_graph=action_graph,
        prepared_value=cast(PreparedShowValue, results[prepare_node_id]),
    )
