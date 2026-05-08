"""Workspace DAG builder for mlody task data-flow dependencies.

This module builds a ``networkx.MultiDiGraph`` from a fully-evaluated
``Workspace``.  Nodes represent tasks (keyed by their canonical all-dict
string ID ``"task/{stem}:{name}"``).  Edges represent value flow between
tasks, annotated with typed ``Edge`` instances.

Graph model
-----------
A single ``Edge`` type covers two wiring modes:
- Input wiring (single-segment ``dst_path``): ``b.inputs["checkpoint"]``
  receives the value from ``a.outputs["checkpoint"]``.
- Config injection (multi-segment ``dst_path``): ``b.action.config.lr``
  is overridden by the value from ``a.outputs["lr"]``.

``dst_path`` convention
~~~~~~~~~~~~~~~~~~~~~~~
``dst_path`` is the dot-separated path on the *consuming* task struct
that identifies where the value lands.  For input wiring it is the bare
value name (e.g. ``"checkpoint"``); for config injection it is a nested
path (e.g. ``"action.config.lr"``).

Workspace lifecycle
~~~~~~~~~~~~~~~~~~~
``build_dag`` must be called after ``Workspace.load()`` has returned
without error.  The graph is a pure function of the evaluated workspace
state; calling it twice produces equivalent graphs (NFR-U-001, §15.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

import networkx

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.task import RegisteredTask
from mlody.common.value import RegisteredValue
from mlody.core.workspace import Workspace

# ── Section 1: Types ─────────────────────────────────────────────────────────


@dataclass
class TaskNode:
    """Mutable metadata for a single task node in the workspace DAG.

    Holds only identity fields plus a live reference to the underlying
    ``RegisteredTask`` dataclass, so traversal passes (e.g., typechecking)
    can replace ``task`` with an annotated copy via ``task.updated(...)``.

    Args:
        node_id: Canonical NetworkX key, e.g. ``"task/stem:name"``.
        name: Bare task name from the ``.mlody`` file (display only).
        task: The ``RegisteredTask`` dataclass for this node.
    """

    node_id: str
    name: str
    task: RegisteredTask


@dataclass
class ValueNode:
    """Mutable metadata for a standalone value node in the workspace DAG.

    Standalone values are registered ``value()`` entities that are not produced
    by any task output port.  They appear as DAG nodes when referenced (directly
    or via a source chain) by a task input or another value.

    Holds a live reference to the underlying ``RegisteredValue`` dataclass so
    traversal passes can annotate it via ``value.updated(...)``.

    Args:
        node_id: Canonical NetworkX key, e.g. ``"value/stem:name"``.
        name: Bare value name from the ``.mlody`` file (display only).
        value: The ``RegisteredValue`` dataclass for this node.
    """

    node_id: str
    name: str
    value: RegisteredValue


@dataclass(frozen=True)
class Edge:
    """Immutable annotation on a directed edge in the workspace DAG.

    A single ``Edge`` covers both input wiring (single-segment ``dst_path``)
    and config injection (multi-segment ``dst_path``).

    Args:
        src_port: Output port name on the source task.
        dst_path: Dot-separated destination path on the consuming task.
    """

    src_port: str
    dst_path: str


@dataclass(frozen=True)
class PortRef:
    """Parsed reference to a named port on a named task.

    Internal use during DAG construction.  Also exported for callers that
    need to use ``parse_port_location`` directly.

    Args:
        task: Bare task name referenced by the location string.
        port: Port (value) name on that task.
    """

    task: str
    port: str


@dataclass(frozen=True)
class PathError:
    """Record of a dst_path validation failure on an edge.

    Args:
        task: Destination task node_id where the error was detected.
        path: The full ``dst_path`` that failed to resolve.
        reason: Human-readable explanation naming the failing segment.
    """

    task: str
    path: str
    reason: str


class PortLocationParseError(ValueError):
    """Raised when ``parse_port_location`` cannot parse the input string."""


# ── Section 2: Port Location Parsing ─────────────────────────────────────────

# Regex per spec §3.2.
# Group 1 — task name: must start with letter/underscore; may contain hyphens.
# Group 2 — port name: same character set, dots allowed for nested port paths.
_PORT_LOCATION_RE = re.compile(
    r"^:([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_.-]*)$"
)


def parse_port_location(raw: str) -> PortRef:
    """Parse a port location string of the form ``:task_name.port_name``.

    The leading colon is required.  Only syntactic parsing is performed;
    no validation against registered tasks is done here.

    Args:
        raw: A string of the form ``:task_name.port_name``.

    Returns:
        A ``PortRef`` with ``task`` and ``port`` fields populated.

    Raises:
        PortLocationParseError: If ``raw`` does not match the expected
            pattern ``^:([A-Za-z_][A-Za-z0-9_-]*)\\.([A-Za-z_][A-Za-z0-9_.-]*)$``.
    """
    m = _PORT_LOCATION_RE.match(raw)
    if m is None:
        msg = f"Invalid port location {raw!r}: expected ':task_name.port_name'"
        raise PortLocationParseError(msg)
    return PortRef(task=m.group(1), port=m.group(2))


# ── Section 3: DAG Construction ───────────────────────────────────────────────


def _iter_tasks(
    evaluator: Evaluator,
) -> Iterator[tuple[str, object]]:
    """Yield ``(node_id, task_struct)`` pairs for every registered task.

    ``node_id`` is ``"task/{stem}:{name}"``, derived from the ``tasks``
    dict key (which is already ``"{stem}:{name}"``).
    """
    for tasks_key, task_struct in evaluator.registry.tasks.by_key.items():
        yield f"task/{tasks_key}", task_struct


def iter_port_values(container: object) -> tuple[RegisteredValue, ...]:
    """Return port values from a name-keyed dict port collection."""
    if isinstance(container, dict):
        return tuple(container.values())  # type: ignore[return-value]
    return ()


def _collect_edges(
    evaluator: Evaluator,
    tasks_index: dict[str, tuple[str, object]],
    output_to_producer: dict[str, str],
    id_to_producer: dict[int, str],
) -> list[tuple[str, str, Edge]]:
    """Scan every task's inputs and outputs for cross-task port references.

    Returns a list of ``(src_node_id, dst_node_id, edge)`` triples.

    Sources are dataclasses after the workspace resolution pass:
    ``RegisteredValue`` for value labels and ``PortRef`` for ``:task.port``
    labels. The id-based index is checked first to correctly handle tasks that
    share an output port name; name-based lookup is the fallback.
    """
    triples: list[tuple[str, str, Edge]] = []

    for tasks_key, task_struct in evaluator.registry.tasks.by_key.items():
        consumer_node_id = f"task/{tasks_key}"

        for port_val in (
            *iter_port_values(getattr(task_struct, "outputs", {})),
            *iter_port_values(getattr(task_struct, "inputs", {})),
        ):
            source_val = getattr(port_val, "source", None)
            if source_val is None:
                continue
            dst_path: str = getattr(port_val, "name", "")
            if isinstance(source_val, PortRef):
                producer = tasks_index.get(source_val.task)
                if producer is not None:
                    triples.append(
                        (producer[0], consumer_node_id, Edge(src_port=source_val.port, dst_path=dst_path))
                    )
                continue
            src_name: str = getattr(source_val, "name", "")
            prod = id_to_producer.get(id(source_val)) or output_to_producer.get(src_name)
            if prod is not None:
                triples.append((prod, consumer_node_id, Edge(src_port=src_name, dst_path=dst_path)))

    return triples


def _resolve_source_name(source_val: object) -> str | None:
    """Extract a value name from a resolved ``source=`` field, or return ``None``."""
    if source_val is None:
        return None
    if isinstance(source_val, PortRef):
        return None
    return getattr(source_val, "name", None) or None


def _build_output_index(
    tasks_index: dict[str, tuple[str, object]],
) -> tuple[dict[str, str], dict[int, str]]:
    """Return output-port lookup indexes for all task outputs.

    Returns:
        A pair ``(by_name, by_id)`` where ``by_name`` maps output port name to
        producer task node ID, and ``by_id`` maps ``id(rv)`` to producer node ID.
        ``by_id`` takes precedence in ``_collect_edges`` to correctly handle tasks
        that share an output port name.
    """
    by_name: dict[str, str] = {}
    by_id: dict[int, str] = {}
    for _task_name, (prod_node_id, prod_struct) in tasks_index.items():
        for v in iter_port_values(getattr(prod_struct, "outputs", {})):
            v_name: str = getattr(v, "name", "")
            if v_name:
                by_name[v_name] = prod_node_id
                by_id[id(v)] = prod_node_id
    return by_name, by_id


def _collect_value_edges(
    evaluator: Evaluator,
    tasks_index: dict[str, tuple[str, object]],
    output_to_producer: dict[str, str],
    id_to_producer: dict[int, str],
) -> tuple[list[tuple[str, object]], list[tuple[str, str, Edge]]]:
    """Collect standalone value nodes and edges for values used as sources.

    Scans every task's input and output ports for ``source=`` references to
    values that are *not* task outputs, then follows each value's own
    ``source=`` chain via BFS.

    Args:
        evaluator: The fully-evaluated workspace evaluator.
        tasks_index: Mapping of bare task name → ``(node_id, task_struct)``.
        output_to_producer: Mapping of output port name → producer task node_id.
        id_to_producer: Mapping of ``id(rv)`` → producer task node_id; used for
            accurate lookup when tasks share an output port name.

    Returns:
        A pair ``(value_nodes, edges)`` where:
        - ``value_nodes`` is a list of ``(node_id, value_struct)`` tuples for
          standalone value nodes to add to the graph.
        - ``edges`` is a list of ``(src_id, dst_id, Edge)`` triples covering
          value→task and value→value (and task→value when a value sources from
          a task output) connections.
    """
    values_by_name: dict[str, tuple[str, object]] = {}
    for values_key, value_struct in evaluator.registry.values.by_key.items():
        v_name: str = getattr(value_struct, "name", "")
        if v_name:
            values_by_name[v_name] = (f"value/{values_key}", value_struct)

    value_nodes: list[tuple[str, object]] = []
    edges: list[tuple[str, str, Edge]] = []
    visited: set[str] = set()

    # Work-queue items: (src_value_name, dst_node_id, dst_path)
    pending: list[tuple[str, str, str]] = []

    for tasks_key, task_struct in evaluator.registry.tasks.by_key.items():
        consumer_id = f"task/{tasks_key}"
        for port_val in (
            *iter_port_values(getattr(task_struct, "outputs", {})),
            *iter_port_values(getattr(task_struct, "inputs", {})),
        ):
            source_val = getattr(port_val, "source", None)
            if source_val is None:
                continue
            if isinstance(source_val, PortRef):
                continue
            dst_path: str = getattr(port_val, "name", "")
            src_name = _resolve_source_name(source_val)
            if src_name is None:
                continue
            is_task_output = (
                id_to_producer.get(id(source_val)) is not None
                or src_name in output_to_producer
            )
            if not is_task_output and src_name in values_by_name:
                pending.append((src_name, consumer_id, dst_path))

    while pending:
        src_name, dst_id, dst_path = pending.pop()

        src_node_id, src_struct = values_by_name[src_name]
        edges.append((src_node_id, dst_id, Edge(src_port=src_name, dst_path=dst_path)))

        if src_name in visited:
            continue
        visited.add(src_name)
        value_nodes.append((src_node_id, src_struct))

        upstream_val = getattr(src_struct, "source", None)
        if isinstance(upstream_val, PortRef):
            producer = tasks_index.get(upstream_val.task)
            if producer is not None:
                edges.append(
                    (producer[0], src_node_id, Edge(src_port=upstream_val.port, dst_path=src_name))
                )
            continue
        upstream_name = _resolve_source_name(upstream_val)
        if upstream_name is None:
            continue

        upstream_is_task_output = (
            id_to_producer.get(id(upstream_val)) is not None
            if upstream_val is not None
            else False
        ) or upstream_name in output_to_producer
        if upstream_is_task_output:
            task_id = (
                id_to_producer.get(id(upstream_val))
                or output_to_producer[upstream_name]
            )
            edges.append((task_id, src_node_id, Edge(src_port=upstream_name, dst_path=src_name)))
        elif upstream_name in values_by_name:
            pending.append((upstream_name, src_node_id, src_name))

    return value_nodes, edges


def build_dag(workspace: Workspace) -> networkx.MultiDiGraph:
    """Build a directed acyclic graph of task data-flow dependencies.

    Iterates over all registered tasks in the evaluated workspace and
    constructs a ``MultiDiGraph`` where nodes are tasks (keyed by their
    all-dict node ID) and edges represent value flow between tasks.

    Must be called after ``Workspace.load()`` has completed without error.
    Calling ``build_dag`` twice on the same workspace produces equivalent
    graphs (pure function of evaluated state).

    Args:
        workspace: A fully-loaded ``Workspace`` instance.

    Returns:
        A ``networkx.MultiDiGraph``.  Each node key is a string of the
        form ``'task/{stem}:{name}'`` or ``'value/{stem}:{name}'``.
        Node data: ``dag.nodes[key]['task']`` is a ``TaskNode`` whose
        ``.task`` field holds the ``RegisteredTask``; ``dag.nodes[key]['value']``
        is a ``ValueNode`` whose ``.value`` field holds the ``RegisteredValue``.
        Edge data: ``dag.edges[src, dst, k]['edge']`` is an ``Edge``.
    """
    evaluator = workspace.evaluator
    dag: networkx.MultiDiGraph = networkx.MultiDiGraph()

    # Step 1: collect task nodes.
    # tasks_index maps bare task name -> (node_id, task_struct) for edge resolution.
    tasks_index: dict[str, tuple[str, object]] = {}

    for node_id, task_struct in _iter_tasks(evaluator):
        task_node = TaskNode(
            node_id=node_id,
            name=getattr(task_struct, "name", ""),  # type: ignore[attr-defined]
            task=task_struct,  # type: ignore[arg-type]
        )
        dag.add_node(node_id, task=task_node)
        bare_name: str = getattr(task_struct, "name", "")  # type: ignore[attr-defined]
        tasks_index[bare_name] = (node_id, task_struct)

    output_to_producer, id_to_producer = _build_output_index(tasks_index)

    # Step 2: collect task→task edges from value labels.
    for src_id, dst_id, edge in _collect_edges(
        evaluator, tasks_index, output_to_producer, id_to_producer
    ):
        dag.add_edge(src_id, dst_id, edge=edge)

    # Step 3: collect standalone value nodes and their edges.
    value_nodes, value_edges = _collect_value_edges(
        evaluator, tasks_index, output_to_producer, id_to_producer
    )
    for val_node_id, val_struct in value_nodes:
        val_node = ValueNode(
            node_id=val_node_id,
            name=getattr(val_struct, "name", ""),  # type: ignore[attr-defined]
            value=val_struct,  # type: ignore[arg-type]
        )
        dag.add_node(val_node_id, value=val_node)
    for src_id, dst_id, edge in value_edges:
        dag.add_edge(src_id, dst_id, edge=edge)

    return dag


# ── Section 4: Query Interface ────────────────────────────────────────────────


def tasks_producing(dag: networkx.MultiDiGraph, value_name: str) -> set[str]:
    """Return the set of node IDs whose output_ports include ``value_name``.

    Performs an O(N) scan over all nodes.

    Args:
        dag: A ``MultiDiGraph`` produced by ``build_dag``.
        value_name: The output port name to search for.

    Returns:
        A set of node ID strings.  Empty if no task produces ``value_name``.
    """
    result: set[str] = set()
    for node_id, node_data in dag.nodes(data=True):
        task_node: TaskNode | None = node_data.get("task")
        if task_node is not None and any(
            getattr(v, "name", "") == value_name
            for v in iter_port_values(getattr(task_node.task, "outputs", None))
        ):
            result.add(node_id)
    return result


def tasks_consuming(dag: networkx.MultiDiGraph, value_name: str) -> set[str]:
    """Return the set of node IDs that have an incoming edge with ``src_port == value_name``.

    Performs an O(E) scan over all edges.

    Args:
        dag: A ``MultiDiGraph`` produced by ``build_dag``.
        value_name: The source port name to search for.

    Returns:
        A set of destination node ID strings.  Empty if no edge uses
        ``value_name`` as its ``src_port``.
    """
    result: set[str] = set()
    for _src, dst, _key, edge_data in dag.edges(data=True, keys=True):
        edge: Edge = edge_data["edge"]
        if edge.src_port == value_name:
            result.add(dst)
    return result


def ancestors_subgraph(
    dag: networkx.MultiDiGraph, target_output: str
) -> networkx.MultiDiGraph:
    """Return the minimal subgraph of tasks that contribute to ``target_output``.

    Uses ``networkx.ancestors()`` to find all transitive predecessors of each
    task that produces ``target_output``, then returns the induced subgraph
    as a copy.

    If no task produces ``target_output``, returns an empty ``MultiDiGraph``.

    Args:
        dag: A ``MultiDiGraph`` produced by ``build_dag``.
        target_output: Output port name to trace back from.

    Returns:
        A new ``networkx.MultiDiGraph`` (copy, not a view) containing only
        the relevant tasks and edges.  Modifications to the returned graph
        do not affect ``dag``.
    """
    producers = tasks_producing(dag, target_output)
    for node_id, node_data in dag.nodes(data=True):
        value_node: ValueNode | None = node_data.get("value")
        if value_node is not None and value_node.name == target_output:
            producers.add(node_id)
    if not producers:
        return networkx.MultiDiGraph()
    all_relevant: set[str] = set(producers)
    for node_id in producers:
        all_relevant |= networkx.ancestors(dag, node_id)
    result: networkx.MultiDiGraph = dag.subgraph(all_relevant).copy()
    return result


def validate_paths(dag: networkx.MultiDiGraph) -> list[PathError]:
    """Validate all ``Edge.dst_path`` values against the destination task struct.

    Traverses each edge in the DAG and checks that ``dst_path`` resolves
    against the task struct's declared fields via ``hasattr``/``getattr``
    traversal.  Does not raise; collects and returns all errors.

    Args:
        dag: A ``MultiDiGraph`` produced by ``build_dag``.

    Returns:
        A list of ``PathError`` instances.  An empty list means no errors
        were found.
    """
    errors: list[PathError] = []

    for _src, dst, _key, edge_data in dag.edges(keys=True, data=True):
        task_node: TaskNode | None = dag.nodes[dst].get("task")
        if task_node is None:
            continue
        edge: Edge = edge_data["edge"]
        dst_path = edge.dst_path
        task_struct = task_node.task

        segments = dst_path.split(".")
        obj: object = task_struct
        for i, segment in enumerate(segments):
            if not hasattr(obj, segment):
                errors.append(
                    PathError(
                        task=dst,
                        path=dst_path,
                        reason=(
                            f"segment {segment!r} not found on"
                            f" {type(obj).__name__} at position {i}"
                            f" in path {dst_path!r}"
                        ),
                    )
                )
                break
            obj = getattr(obj, segment)

    return errors
