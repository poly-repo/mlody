"""Shared show planning and execution helpers for console and stage."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import logging
from types import MappingProxyType
from typing import cast

import networkx
import pyarrow as pa

from mlody.core.action_graph import (
    ActionGraphSelection,
    MlodyActionGraphNode,
    build_action_graph,
    selection_for_label,
)
from mlody.core.derived import DerivedValueShapeError
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular.location_specs import source_from_value
from mlody.core.tabular.derived_source import DerivedSource
from mlody.core.workspace import Workspace, force
from mlody.resolver import (
    MlodyValue,
    MlodyValueValue,
    MlodyVectorValue,
    resolve_label_to_value,
)

_logger = logging.getLogger(__name__)

ShowActionNodeCallable = Callable[["ShowActionExecutionContext"], object]


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


PreparedShowResultFinalizer = Callable[
    [PreparedShowValue, "ShowActionExecutionContext"],
    object,
]


@dataclass(frozen=True)
class CliPreparedShowResult:
    """CLI-facing artifact produced by the ``prepare display`` action."""

    value: MlodyValue
    prepared: PreparedShowValue


@dataclass(frozen=True)
class StagePreparedShowResult:
    """Stage-facing artifact produced by the ``prepare display`` action."""

    value: MlodyValue
    prepared: PreparedShowValue
    stage_result: dict[str, object]


@dataclass(frozen=True)
class ShowActionStubResult:
    """Placeholder result for actions that do not execute real work yet."""

    node_id: str
    operation: str
    title: str
    detail: str | None = None


@dataclass(frozen=True)
class ShowActionExecutionContext:
    """Runtime inputs visible to one action-graph node callable."""

    workspace: Workspace
    selection: ActionGraphSelection
    action_graph: networkx.DiGraph
    requested_label: str
    concrete_label: object
    resolved_value: MlodyValue
    action: MlodyActionGraphNode
    dependency_results: Mapping[str, object]
    node_results: Mapping[str, object]


@dataclass(frozen=True)
class ShowActionGraphExecution:
    """Result of binding and executing a ``show`` action graph."""

    selection: ActionGraphSelection
    action_graph: networkx.DiGraph
    node_results: Mapping[str, object]
    final_result: object


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


def _make_prepare_display_callable(
    finalizer: PreparedShowResultFinalizer,
    *,
    display_value: Callable[[MlodyValueValue], object] = _default_display_payload,
    db_conn: object | None = None,
) -> ShowActionNodeCallable:
    def _run(context: ShowActionExecutionContext) -> object:
        prepared = prepare_show_value(
            context.resolved_value,
            display_value=display_value,
            db_conn=db_conn,
        )
        return finalizer(prepared, context)

    return _run


def make_cli_prepare_display(
    *,
    display_value: Callable[[MlodyValueValue], object] = _default_display_payload,
    db_conn: object | None = None,
) -> ShowActionNodeCallable:
    return _make_prepare_display_callable(
        lambda prepared, _context: CliPreparedShowResult(
            value=prepared.value,
            prepared=prepared,
        ),
        display_value=display_value,
        db_conn=db_conn,
    )


def make_stage_prepare_display(
    *,
    stage_result_builder: Callable[[MlodyValue, PreparedShowValue], dict[str, object]],
    display_value: Callable[[MlodyValueValue], object] = _default_display_payload,
    db_conn: object | None = None,
) -> ShowActionNodeCallable:
    return _make_prepare_display_callable(
        lambda prepared, _context: StagePreparedShowResult(
            value=prepared.value,
            prepared=prepared,
            stage_result=stage_result_builder(prepared.value, prepared),
        ),
        display_value=display_value,
        db_conn=db_conn,
    )


def _stub_show_action_callable(action: MlodyActionGraphNode) -> ShowActionNodeCallable:
    def _run(_context: ShowActionExecutionContext) -> ShowActionStubResult:
        _logger.info(
            "Show action %s (%s) would run once implemented",
            action.node_id,
            action.operation,
        )
        return ShowActionStubResult(
            node_id=action.node_id,
            operation=action.operation,
            title=action.title,
            detail=action.detail,
        )

    return _run


def _action_result_summary(result: object) -> str:
    if isinstance(result, CliPreparedShowResult):
        return (
            f"{type(result).__name__}(value={type(result.value).__name__}, "
            f"prepared={type(result.prepared).__name__})"
        )
    if isinstance(result, StagePreparedShowResult):
        return (
            f"{type(result).__name__}(value={type(result.value).__name__}, "
            f"prepared={type(result.prepared).__name__}, "
            f"stage_result={type(result.stage_result).__name__})"
        )
    if isinstance(result, ShowActionStubResult):
        return f"{type(result).__name__}(operation={result.operation})"
    if isinstance(result, PreparedShowValue):
        return f"{type(result).__name__}(value={type(result.value).__name__})"
    if isinstance(result, str):
        return repr(result)
    return type(result).__name__


def _bind_show_action_callables(
    action_graph: networkx.DiGraph,
    *,
    prepare_display: ShowActionNodeCallable,
) -> None:
    prepare_node_id = cast(str, action_graph.graph["prepare_node_id"])
    for node_id in networkx.topological_sort(action_graph):
        action = cast(MlodyActionGraphNode, action_graph.nodes[node_id]["action"])
        if action.executor != "mlody":
            raise ValueError(f"Unsupported show executor: {action.executor!r}")
        node_callable = (
            prepare_display if node_id == prepare_node_id else _stub_show_action_callable(action)
        )
        action_graph.nodes[node_id]["action"] = replace(
            action,
            callable=node_callable,
        )


def execute_show_action_graph(
    workspace: Workspace,
    requested_label: str,
    concrete_label: object,
    *,
    resolve_label: Callable[[object, Workspace], MlodyValue] = resolve_label_to_value,
    prepare_display: ShowActionNodeCallable,
) -> ShowActionGraphExecution:
    resolved_value = resolve_label(concrete_label, workspace)
    selection = selection_for_label(workspace, requested_label)
    action_graph = build_action_graph(selection)
    _bind_show_action_callables(
        action_graph,
        prepare_display=prepare_display,
    )
    results: dict[str, object] = {}

    for node_id in networkx.topological_sort(action_graph):
        action = cast(MlodyActionGraphNode, action_graph.nodes[node_id]["action"])
        if action.callable is None:
            raise ValueError(f"Show action {node_id!r} is missing a callable")
        dependency_results = {
            src_id: results[src_id]
            for src_id, _dst_id in action_graph.in_edges(node_id)
        }
        results[node_id] = action.callable(
            ShowActionExecutionContext(
                workspace=workspace,
                selection=selection,
                action_graph=action_graph,
                requested_label=requested_label,
                concrete_label=concrete_label,
                resolved_value=resolved_value,
                action=action,
                dependency_results=MappingProxyType(dependency_results),
                node_results=MappingProxyType(dict(results)),
            )
        )
        _logger.info(
            "Show action %s (%s) produced %s",
            action.node_id,
            action.operation,
            _action_result_summary(results[node_id]),
        )

    prepare_node_id = cast(str, action_graph.graph["prepare_node_id"])
    return ShowActionGraphExecution(
        selection=selection,
        action_graph=action_graph,
        node_results=MappingProxyType(dict(results)),
        final_result=results[prepare_node_id],
    )
