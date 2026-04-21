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
from mlody.core.workspace import Workspace

# ── Section 1: Types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskNode:
    """Immutable metadata for a single task node in the workspace DAG.

    Args:
        node_id: Canonical NetworkX key, e.g. ``"task/stem:name"``.
        name: Bare task name from the ``.mlody`` file (display only).
        action: Action name extracted from the resolved action struct.
        input_ports: Value names declared as inputs on this task.
        output_ports: Value names declared as outputs on this task.
    """

    node_id: str
    name: str
    action: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]


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
    for tasks_key, task_struct in evaluator.tasks.items():
        yield f"task/{tasks_key}", task_struct


def _collect_edges(
    evaluator: Evaluator,
    tasks_index: dict[str, tuple[str, object]],
) -> list[tuple[str, str, Edge]]:
    """Scan every task's inputs and outputs for cross-task port references.

    Returns a list of ``(src_node_id, dst_node_id, edge)`` triples.

    The two-loop algorithm (spec §3.3):
    1. For each output value whose ``label`` starts with ``":"`` and resolves
       via ``parse_port_location``: the *current* task is the consumer, the
       referenced task is the producer.  Edge: producer → consumer.
    2. For each input value whose ``label`` starts with ``":"`` and resolves:
       same direction — producer → current task.
    """
    triples: list[tuple[str, str, Edge]] = []

    for tasks_key, task_struct in evaluator.tasks.items():
        consumer_node_id = f"task/{tasks_key}"

        for out_val in getattr(task_struct, "outputs", []):  # type: ignore[attr-defined]
            label = getattr(out_val, "source", None)  # type: ignore[attr-defined]
            if not isinstance(label, str) or not label.startswith(":"):
                continue
            try:
                ref = parse_port_location(label)
            except PortLocationParseError:
                continue
            if ref.task not in tasks_index:
                continue
            producer_node_id, _ = tasks_index[ref.task]
            edge = Edge(src_port=ref.port, dst_path=getattr(out_val, "name", ""))  # type: ignore[attr-defined]
            triples.append((producer_node_id, consumer_node_id, edge))

        for in_val in getattr(task_struct, "inputs", []):  # type: ignore[attr-defined]
            label = getattr(in_val, "source", None)  # type: ignore[attr-defined]
            if not isinstance(label, str) or not label.startswith(":"):
                continue
            try:
                ref = parse_port_location(label)
            except PortLocationParseError:
                continue
            if ref.task not in tasks_index:
                continue
            producer_node_id, _ = tasks_index[ref.task]
            edge = Edge(src_port=ref.port, dst_path=getattr(in_val, "name", ""))  # type: ignore[attr-defined]
            triples.append((producer_node_id, consumer_node_id, edge))

    return triples


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
        form ``'task/{stem}:{name}'``.  Node data:
        ``dag.nodes[key]['task']`` is a ``TaskNode``;
        ``dag.nodes[key]['task_struct']`` is the raw ``Struct`` from the
        evaluator (for ``validate_paths``).  Edge data:
        ``dag.edges[src, dst, k]['edge']`` is an ``Edge``.
    """
    evaluator = workspace.evaluator
    dag: networkx.MultiDiGraph = networkx.MultiDiGraph()

    # Step 1: collect task nodes.
    # tasks_index maps bare task name -> (node_id, task_struct) for edge resolution.
    tasks_index: dict[str, tuple[str, object]] = {}

    for node_id, task_struct in _iter_tasks(evaluator):
        action_obj = getattr(task_struct, "action", None)  # type: ignore[attr-defined]
        action_name: str = getattr(action_obj, "name", str(action_obj))  # type: ignore[attr-defined]
        input_ports = tuple(
            getattr(v, "name", "") for v in getattr(task_struct, "inputs", [])  # type: ignore[attr-defined]
        )
        output_ports = tuple(
            getattr(v, "name", "") for v in getattr(task_struct, "outputs", [])  # type: ignore[attr-defined]
        )
        task_node = TaskNode(
            node_id=node_id,
            name=getattr(task_struct, "name", ""),  # type: ignore[attr-defined]
            action=action_name,
            input_ports=input_ports,
            output_ports=output_ports,
        )
        dag.add_node(node_id, task=task_node, task_struct=task_struct)
        bare_name: str = getattr(task_struct, "name", "")  # type: ignore[attr-defined]
        tasks_index[bare_name] = (node_id, task_struct)

    # Step 2: collect edges from value labels.
    for src_id, dst_id, edge in _collect_edges(evaluator, tasks_index):
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
        task_node: TaskNode = node_data["task"]
        if value_name in task_node.output_ports:
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
    if not producers:
        return networkx.MultiDiGraph()
    all_relevant: set[str] = set(producers)
    for task_id in producers:
        all_relevant |= networkx.ancestors(dag, task_id)
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
        edge: Edge = edge_data["edge"]
        dst_path = edge.dst_path
        task_struct = dag.nodes[dst]["task_struct"]

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
