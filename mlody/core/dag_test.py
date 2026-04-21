"""Tests for mlody.core.dag — workspace DAG builder.

All tests trace back to named requirements and scenarios in
mlody/openspec/changes/mav-437-workspace-dag/SPEC.md and REQUIREMENTS.md.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from textwrap import dedent

import networkx
import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
from mlody.core.dag import (
    Edge,
    PathError,
    PortLocationParseError,
    PortRef,
    TaskNode,
    ancestors_subgraph,
    build_dag,
    parse_port_location,
    tasks_consuming,
    tasks_producing,
    validate_paths,
)

# ---------------------------------------------------------------------------
# Base .mlody file set for task/value/action tests
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR.parent / "common" / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR.parent / "common" / "types.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR.parent / "common" / "locations.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR.parent / "common" / "values.mlody").read_text()
_ACTION_MLODY = (_THIS_DIR.parent / "common" / "action.mlody").read_text()
_TASK_MLODY = (_THIS_DIR.parent / "common" / "task.mlody").read_text()

BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
    "mlody/common/task.mlody": _TASK_MLODY,
}

_PREAMBLE = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/values.mlody")\n'
    'load("//mlody/common/action.mlody")\n'
    'load("//mlody/common/task.mlody")\n'
)


class _FakeWorkspace:
    """Minimal workspace stub for tests — exposes only .evaluator."""

    def __init__(self, evaluator: Evaluator) -> None:
        self._evaluator = evaluator

    @property
    def evaluator(self) -> Evaluator:
        return self._evaluator


def _build_dag_from_mlody(files: dict[str, str]) -> networkx.MultiDiGraph:
    """Build a DAG from a dict of additional .mlody file contents.

    'test.mlody' in the dict is the entry point; BASE_FILES are pre-merged.
    """
    with InMemoryFS(BASE_FILES | files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    ws = _FakeWorkspace(ev)
    return build_dag(ws)


# ---------------------------------------------------------------------------
# Section 1 — Types (Task 2)
# ---------------------------------------------------------------------------


class TestTaskNodeIsFrozen:
    """FR: TaskNode must be a frozen dataclass (spec §3.1)."""

    def test_task_node_is_frozen(self) -> None:
        node = TaskNode(
            node_id="task/test:n",
            name="n",
            action="a",
            input_ports=("x",),
            output_ports=("y",),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.name = "other"  # type: ignore[misc]


class TestEdgeIsFrozen:
    """FR: Edge must be a frozen dataclass (spec §3.1)."""

    def test_edge_is_frozen(self) -> None:
        edge = Edge(src_port="out", dst_path="inp")
        with pytest.raises(dataclasses.FrozenInstanceError):
            edge.src_port = "other"  # type: ignore[misc]


class TestPortRefIsFrozen:
    """FR: PortRef must be a frozen dataclass (spec §3.1)."""

    def test_port_ref_is_frozen(self) -> None:
        ref = PortRef(task="t", port="p")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.task = "other"  # type: ignore[misc]


class TestPathErrorIsFrozen:
    """FR: PathError must be a frozen dataclass (spec §3.1)."""

    def test_path_error_is_frozen(self) -> None:
        err = PathError(task="task/s:n", path="a.b", reason="missing b")
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.task = "other"  # type: ignore[misc]


class TestPortLocationParseErrorIsValueError:
    """FR: PortLocationParseError must be a subclass of ValueError (spec §3.1)."""

    def test_port_location_parse_error_is_value_error(self) -> None:
        assert issubclass(PortLocationParseError, ValueError)


class TestTaskNodeFields:
    """FR: TaskNode fields match spec §3.1."""

    def test_task_node_fields(self) -> None:
        node = TaskNode(
            node_id="task/s:n",
            name="n",
            action="a",
            input_ports=("x",),
            output_ports=("y",),
        )
        assert node.node_id == "task/s:n"
        assert node.name == "n"
        assert node.action == "a"
        assert node.input_ports == ("x",)
        assert node.output_ports == ("y",)


# ---------------------------------------------------------------------------
# Section 2 — parse_port_location (Task 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (":a.outputs", PortRef(task="a", port="outputs")),
        (":pretrain.checkpoint", PortRef(task="pretrain", port="checkpoint")),
        (":my-task.out_val", PortRef(task="my-task", port="out_val")),
        # dots allowed in port group 2
        (":t.nested.port", PortRef(task="t", port="nested.port")),
    ],
)
class TestParsePortLocationValid:
    """FR-007: parse_port_location parses valid strings."""

    def test_valid(self, raw: str, expected: PortRef) -> None:
        assert parse_port_location(raw) == expected


class TestParsePortLocationMissingColon:
    """FR-007: missing leading colon raises PortLocationParseError."""

    def test_missing_colon(self) -> None:
        with pytest.raises(PortLocationParseError):
            parse_port_location("a.outputs")


class TestParsePortLocationMissingDot:
    """FR-007: string without dot raises PortLocationParseError."""

    def test_missing_dot(self) -> None:
        with pytest.raises(PortLocationParseError):
            parse_port_location(":a")


class TestParsePortLocationEmpty:
    """FR-007: empty string raises PortLocationParseError."""

    def test_empty(self) -> None:
        with pytest.raises(PortLocationParseError):
            parse_port_location("")


class TestParsePortLocationBadTaskName:
    """FR-007: task name starting with digit raises PortLocationParseError."""

    def test_bad_task_name(self) -> None:
        with pytest.raises(PortLocationParseError):
            parse_port_location(":123task.port")


# ---------------------------------------------------------------------------
# Section 3 — build_dag: node construction (Task 4)
# ---------------------------------------------------------------------------


_ISOLATED_TASK_MLODY = _PREAMBLE + dedent("""\
action(name="act", inputs=[], outputs=[], implementation="dummy")
task(name="solo", inputs=[], outputs=[], action="act")
""")

_TWO_ISOLATED_TASKS_MLODY = _PREAMBLE + dedent("""\
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="alpha", inputs=[], outputs=[], action="act_a")
task(name="beta",  inputs=[], outputs=[], action="act_b")
""")


class TestBuildDagIsolatedTask:
    """FR-002: isolated task appears as node with no edges."""

    def test_isolated_task_one_node_zero_edges(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _ISOLATED_TASK_MLODY})
        assert dag.number_of_nodes() == 1
        assert dag.number_of_edges() == 0
        node_id = "task/test:solo"
        assert node_id in dag.nodes
        task_node: TaskNode = dag.nodes[node_id]["task"]
        assert task_node.name == "solo"
        assert task_node.action == "act"
        assert task_node.input_ports == ()
        assert task_node.output_ports == ()


class TestBuildDagNodeMetadata:
    """US-002: dag.nodes[n]['task'] is a TaskNode; 'task_struct' is the raw struct."""

    def test_node_metadata(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _ISOLATED_TASK_MLODY})
        node_id = "task/test:solo"
        task_node: TaskNode = dag.nodes[node_id]["task"]
        assert task_node.node_id == node_id
        # task_struct must be the raw struct from the evaluator
        task_struct = dag.nodes[node_id]["task_struct"]
        assert task_struct is not None
        assert getattr(task_struct, "kind", None) == "task"


class TestBuildDagTwoIsolatedTasks:
    """FR-002: two isolated tasks → 2 nodes, 0 edges."""

    def test_two_isolated_tasks(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TWO_ISOLATED_TASKS_MLODY})
        assert dag.number_of_nodes() == 2
        assert dag.number_of_edges() == 0


# ---------------------------------------------------------------------------
# Section 3 — build_dag: edge construction (Task 5)
# ---------------------------------------------------------------------------

# Helper: build a linear A->B->C workspace where B.input has source=":a.out_a"
# and C.input has source=":b.out_b".
# Producer output values (out_a, out_b) have no source — they are the origin.
# Consumer input values (in_b, in_c) carry the source referencing the producer.
_LINEAR_CHAIN_MLODY = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="out_b", type=integer(), location=s3())
value(name="in_b",  type=integer(), location=s3(), source=":a.out_a")
value(name="in_c",  type=integer(), location=s3(), source=":b.out_b")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
action(name="act_c", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],        outputs=["out_a"],  action="act_a")
task(name="b", inputs=["in_b"],  outputs=["out_b"],  action="act_b")
task(name="c", inputs=["in_c"],  outputs=[],         action="act_c")
""")


class TestBuildDagLinearChain:
    """FR-001, FR-002, US-001/002: A->B->C linear chain."""

    def test_linear_chain_three_nodes_two_edges(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _LINEAR_CHAIN_MLODY})
        assert dag.number_of_nodes() == 3
        assert dag.number_of_edges() == 2

    def test_linear_chain_edge_a_to_b(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _LINEAR_CHAIN_MLODY})
        a_id = "task/test:a"
        b_id = "task/test:b"
        assert dag.has_edge(a_id, b_id)

    def test_linear_chain_edge_b_to_c(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _LINEAR_CHAIN_MLODY})
        b_id = "task/test:b"
        c_id = "task/test:c"
        assert dag.has_edge(b_id, c_id)


class TestBuildDagFork:
    """FR-003: A produces value consumed by both B and C (fork)."""

    def test_fork_three_nodes_two_edges(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="val",  type=integer(), location=s3())
value(name="in_b", type=integer(), location=s3(), source=":a.val")
value(name="in_c", type=integer(), location=s3(), source=":a.val")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
action(name="act_c", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],        outputs=["val"],  action="act_a")
task(name="b", inputs=["in_b"],  outputs=[],       action="act_b")
task(name="c", inputs=["in_c"],  outputs=[],       action="act_c")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        c_id = "task/test:c"
        assert dag.number_of_nodes() == 3
        assert dag.number_of_edges() == 2
        assert dag.has_edge(a_id, b_id)
        assert dag.has_edge(a_id, c_id)


class TestBuildDagJoin:
    """FR-001: A and B each produce a value consumed by C (join)."""

    def test_join_three_nodes_two_edges(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="out_b", type=integer(), location=s3())
value(name="in_c1", type=integer(), location=s3(), source=":a.out_a")
value(name="in_c2", type=integer(), location=s3(), source=":b.out_b")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
action(name="act_c", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],              outputs=["out_a"],  action="act_a")
task(name="b", inputs=[],              outputs=["out_b"],  action="act_b")
task(name="c", inputs=["in_c1","in_c2"], outputs=[],       action="act_c")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        c_id = "task/test:c"
        assert dag.number_of_nodes() == 3
        assert dag.number_of_edges() == 2
        assert dag.has_edge(a_id, c_id)
        assert dag.has_edge(b_id, c_id)


class TestBuildDagDiamond:
    """FR-001: A->B->D and A->C->D (diamond topology, 4 nodes 4 edges)."""

    def test_diamond_four_nodes_four_edges(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="out_a",  type=integer(), location=s3())
value(name="in_b",   type=integer(), location=s3(), source=":a.out_a")
value(name="in_c",   type=integer(), location=s3(), source=":a.out_a")
value(name="out_b",  type=integer(), location=s3())
value(name="out_c",  type=integer(), location=s3())
value(name="in_d1",  type=integer(), location=s3(), source=":b.out_b")
value(name="in_d2",  type=integer(), location=s3(), source=":c.out_c")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
action(name="act_c", inputs=[], outputs=[], implementation="dummy")
action(name="act_d", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],               outputs=["out_a"],  action="act_a")
task(name="b", inputs=["in_b"],         outputs=["out_b"],  action="act_b")
task(name="c", inputs=["in_c"],         outputs=["out_c"],  action="act_c")
task(name="d", inputs=["in_d1","in_d2"], outputs=[],        action="act_d")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        assert dag.number_of_nodes() == 4
        assert dag.number_of_edges() == 4


class TestBuildDagMultiEdgeSamePair:
    """FR-003: Two distinct values both consumed by B — two parallel edges (MultiDiGraph)."""

    def test_multi_edge_same_pair(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="out1", type=integer(), location=s3())
value(name="out2", type=integer(), location=s3())
value(name="in1",  type=integer(), location=s3(), source=":a.out1")
value(name="in2",  type=integer(), location=s3(), source=":a.out2")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],            outputs=["out1","out2"], action="act_a")
task(name="b", inputs=["in1","in2"], outputs=[],              action="act_b")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        assert dag.number_of_nodes() == 2
        assert dag.number_of_edges() == 2
        # Both edges are distinct (parallel) from a to b
        edges = list(dag.edges(nbunch=a_id, data=True))
        src_ports = {data["edge"].src_port for _, _, data in edges}
        assert len(src_ports) == 2


class TestBuildDagEdgeAnnotations:
    """US-003: dag.edges[a, b, k]['edge'] is an Edge with non-empty src_port and dst_path."""

    def test_edge_annotation(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="in_b",  type=integer(), location=s3(), source=":a.out_a")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],       outputs=["out_a"], action="act_a")
task(name="b", inputs=["in_b"], outputs=[],        action="act_b")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        edges = list(dag.edges(nbunch=a_id, data=True))
        assert len(edges) == 1
        _, _, data = edges[0]
        edge: Edge = data["edge"]
        assert isinstance(edge, Edge)
        assert edge.src_port != ""
        assert edge.dst_path != ""


class TestBuildDagSingleSegmentDstPath:
    """FR-005, FR-008: single-segment dst_path (plain input port name)."""

    def test_single_segment_dst_path(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="model_weight", type=integer(), location=s3())
value(name="model_in",     type=integer(), location=s3(), source=":a.model_weight")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],            outputs=["model_weight"], action="act_a")
task(name="b", inputs=["model_in"],  outputs=[],              action="act_b")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        edges = list(dag.edges(nbunch=a_id, data=True))
        assert len(edges) == 1
        _, _, data = edges[0]
        edge: Edge = data["edge"]
        assert edge.dst_path == "model_in"


class TestBuildDagMultiSegmentDstPath:
    """FR-005, FR-008: multi-segment dst_path on port with dots in the port name.

    The port name :a.action.config.lr is parsed as task='a', port='action.config.lr'.
    That becomes dst_path='action.config.lr' on the consuming task.
    """

    def test_multi_segment_dst_path(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="lr", type=integer(), location=s3(), source=":a.action.config.lr")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],     outputs=[],    action="act_a")
task(name="b", inputs=["lr"], outputs=[],    action="act_b")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        a_id = "task/test:a"
        b_id = "task/test:b"
        edges = list(dag.edges(nbunch=a_id, data=True))
        assert len(edges) == 1
        _, _, data = edges[0]
        edge: Edge = data["edge"]
        assert edge.dst_path == "lr"
        assert edge.src_port == "action.config.lr"


# ---------------------------------------------------------------------------
# Section 4 — Query functions (Task 6)
# ---------------------------------------------------------------------------


_TRAINER_MLODY = _PREAMBLE + dedent("""\
value(name="model", type=integer(), location=s3())
action(name="act", inputs=[], outputs=[], implementation="dummy")
task(name="trainer", inputs=[], outputs=["model"], action="act")
""")

_SOLO_MLODY = _PREAMBLE + dedent("""\
action(name="act", inputs=[], outputs=[], implementation="dummy")
task(name="solo", inputs=[], outputs=[], action="act")
""")


class TestTasksProducingKnown:
    """FR-011, US-005: tasks_producing returns node IDs with matching output port."""

    def test_tasks_producing_known(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TRAINER_MLODY})
        result = tasks_producing(dag, "model")
        assert result == {"task/test:trainer"}


class TestTasksProducingUnknown:
    """FR-011: tasks_producing returns empty set for unknown port."""

    def test_tasks_producing_unknown(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TRAINER_MLODY})
        assert tasks_producing(dag, "nonexistent") == set()


class TestTasksProducingMultiple:
    """FR-011: two tasks each declaring separate output ports."""

    def test_tasks_producing_multiple(self) -> None:
        mlody = _PREAMBLE + dedent("""\
value(name="ckpt_a", type=integer(), location=s3())
value(name="ckpt_b", type=integer(), location=s3())
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="ta", inputs=[], outputs=["ckpt_a"], action="act_a")
task(name="tb", inputs=[], outputs=["ckpt_b"], action="act_b")
""")
        dag = _build_dag_from_mlody({"test.mlody": mlody})
        assert "task/test:ta" in tasks_producing(dag, "ckpt_a")
        assert "task/test:tb" in tasks_producing(dag, "ckpt_b")


_CONSUMING_SINGLE_MLODY = _PREAMBLE + dedent("""\
value(name="tokens", type=integer(), location=s3())
value(name="in_b",   type=integer(), location=s3(), source=":a.tokens")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],       outputs=["tokens"], action="act_a")
task(name="b", inputs=["in_b"], outputs=[],         action="act_b")
""")

_CONSUMING_MULTI_MLODY = _PREAMBLE + dedent("""\
value(name="ws", type=integer(), location=s3(), source=":a.weights.sub")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],     outputs=[], action="act_a")
task(name="b", inputs=["ws"], outputs=[], action="act_b")
""")


class TestTasksConsumingBothPathForms:
    """FR-012, US-006: tasks_consuming checks src_port on incoming edges."""

    def test_consuming_single_segment_src_port(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _CONSUMING_SINGLE_MLODY})
        result = tasks_consuming(dag, "tokens")
        assert result == {"task/test:b"}

    def test_consuming_multi_segment_src_port(self) -> None:
        # port label ":a.weights.sub" → src_port="weights.sub"
        dag = _build_dag_from_mlody({"test.mlody": _CONSUMING_MULTI_MLODY})
        result = tasks_consuming(dag, "weights.sub")
        assert result == {"task/test:b"}


class TestTasksConsumingUnknown:
    """FR-012: tasks_consuming returns empty set for unknown src_port."""

    def test_consuming_unknown(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _SOLO_MLODY})
        assert tasks_consuming(dag, "nonexistent") == set()


# ---------------------------------------------------------------------------
# Section 4 — ancestors_subgraph (Task 7)
# ---------------------------------------------------------------------------


class TestAncestorsSubgraphChain:
    """FR-013, US-007: ancestors_subgraph for C's output includes all three nodes."""

    def test_chain_includes_all_ancestors(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _LINEAR_CHAIN_MLODY})
        sub = ancestors_subgraph(dag, "out_b")
        # out_b is produced by "b"; its ancestor is "a"; "c" is not involved
        assert "task/test:a" in sub.nodes
        assert "task/test:b" in sub.nodes
        assert sub.number_of_edges() == 1


_UNRELATED_MLODY = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="in_b",  type=integer(), location=s3(), source=":a.out_a")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
action(name="act_d", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],       outputs=["out_a"], action="act_a")
task(name="b", inputs=["in_b"], outputs=[],        action="act_b")
task(name="d", inputs=[],       outputs=[],        action="act_d")
""")


class TestAncestorsSubgraphExcludesUnrelated:
    """FR-013: isolated task D not in subgraph when computing C's ancestors."""

    def test_excludes_unrelated_task(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _UNRELATED_MLODY})
        sub = ancestors_subgraph(dag, "out_a")
        assert "task/test:d" not in sub.nodes
        assert "task/test:a" in sub.nodes


class TestAncestorsSubgraphSingleTask:
    """FR-013: single task that produces the target output → 1 node, 0 edges."""

    def test_single_task_subgraph(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TRAINER_MLODY})
        sub = ancestors_subgraph(dag, "model")
        assert sub.number_of_nodes() == 1
        assert sub.number_of_edges() == 0


class TestAncestorsSubgraphNoProducer:
    """FR-013: target output not produced → empty MultiDiGraph returned."""

    def test_no_producer_returns_empty(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _SOLO_MLODY})
        sub = ancestors_subgraph(dag, "nonexistent")
        assert sub.number_of_nodes() == 0
        assert sub.number_of_edges() == 0


class TestAncestorsSubgraphReturnsCopy:
    """Spec §3.4: modifying returned subgraph must not affect the original DAG."""

    def test_returns_copy(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TRAINER_MLODY})
        original_node_count = dag.number_of_nodes()
        sub = ancestors_subgraph(dag, "model")
        sub.add_node("extra_node_for_test")
        # original DAG must be unaffected
        assert dag.number_of_nodes() == original_node_count


class TestTopologicalSortCompatible:
    """FR-014: networkx.topological_sort succeeds on a valid acyclic workspace."""

    def test_topological_sort_no_exception(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _LINEAR_CHAIN_MLODY})
        result = list(networkx.topological_sort(dag))
        node_ids = {dag.nodes[n]["task"].node_id for n in dag.nodes}
        assert set(result) == node_ids


# ---------------------------------------------------------------------------
# Section 4 — validate_paths (Task 8)
# ---------------------------------------------------------------------------

_VALID_PATHS_MLODY = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="inputs", type=integer(), location=s3(), source=":a.out_a")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],         outputs=["out_a"],  action="act_a")
task(name="b", inputs=["inputs"], outputs=[],         action="act_b")
""")

_INVALID_PATH_MLODY = _PREAMBLE + dedent("""\
value(name="out_a",          type=integer(), location=s3())
value(name="nonexistent_field", type=integer(), location=s3(), source=":a.out_a")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],                    outputs=["out_a"], action="act_a")
task(name="b", inputs=["nonexistent_field"], outputs=[],        action="act_b")
""")

_TWO_INVALID_PATHS_MLODY = _PREAMBLE + dedent("""\
value(name="out_a", type=integer(), location=s3())
value(name="bad1", type=integer(), location=s3(), source=":a.out_a")
value(name="bad2", type=integer(), location=s3(), source=":a.out_a")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],              outputs=["out_a"], action="act_a")
task(name="b", inputs=["bad1","bad2"], outputs=[],        action="act_b")
""")

_MULTI_SEGMENT_PATH_MLODY = _PREAMBLE + dedent("""\
value(name="lr", type=integer(), location=s3(), source=":a.action.config.lr")
action(name="act_a", inputs=[], outputs=[], implementation="dummy")
action(name="act_b", inputs=[], outputs=[], implementation="dummy")
task(name="a", inputs=[],     outputs=[],  action="act_a")
task(name="b", inputs=["lr"], outputs=[],  action="act_b")
""")


class TestValidatePathsValid:
    """FR-009: validate_paths returns [] when all dst_paths resolve."""

    def test_valid_paths_return_empty_list(self) -> None:
        # dst_path "inputs" is a known field on the task struct
        dag = _build_dag_from_mlody({"test.mlody": _VALID_PATHS_MLODY})
        errors = validate_paths(dag)
        assert errors == []


class TestValidatePathsInvalidSegment:
    """FR-009, FR-010, NFR-U-002: invalid dst_path returns a PathError naming the task."""

    def test_invalid_segment_returns_path_error(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _INVALID_PATH_MLODY})
        errors = validate_paths(dag)
        assert len(errors) == 1
        err = errors[0]
        assert isinstance(err, PathError)
        assert err.task == "task/test:b"
        assert err.path == "nonexistent_field"
        assert "nonexistent_field" in err.reason


class TestValidatePathsMultipleErrors:
    """FR-009: all errors collected — no early exit."""

    def test_two_invalid_edges_two_errors(self) -> None:
        dag = _build_dag_from_mlody({"test.mlody": _TWO_INVALID_PATHS_MLODY})
        errors = validate_paths(dag)
        # bad1 and bad2 are not fields on the task struct
        assert len(errors) == 2


class TestValidatePathsMultiSegment:
    """FR-009: multi-segment dst_path names failing segment correctly."""

    def test_multi_segment_names_failing_segment(self) -> None:
        # dst_path="lr" where "lr" is not a field on the task struct
        dag = _build_dag_from_mlody({"test.mlody": _MULTI_SEGMENT_PATH_MLODY})
        errors = validate_paths(dag)
        # dst_path is "lr" which is not on the task struct
        assert len(errors) == 1
        assert errors[0].path == "lr"
        assert "lr" in errors[0].reason
