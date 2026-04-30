"""Integration tests for mlody/common/executor.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_FRESHNESS_MLODY = (_THIS_DIR / "freshness.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_REPRESENTATION_MLODY = (_THIS_DIR / "representation.mlody").read_text()
_BUILD_REF_MLODY = (_THIS_DIR / "build_ref.mlody").read_text()
_IMPLEMENTATION_MLODY = (_THIS_DIR / "implementation.mlody").read_text()
_EXECUTOR_MLODY = (_THIS_DIR / "executor.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()
_ACTION_MLODY = (_THIS_DIR / "action.mlody").read_text()
_TASK_MLODY = (_THIS_DIR / "task.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/freshness.mlody": _FRESHNESS_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/representation.mlody": _REPRESENTATION_MLODY,
    "mlody/common/build_ref.mlody": _BUILD_REF_MLODY,
    "mlody/common/implementation.mlody": _IMPLEMENTATION_MLODY,
    "mlody/common/executor.mlody": _EXECUTOR_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
    "mlody/common/task.mlody": _TASK_MLODY,
}

# executor.mlody injects host/kubernetes/kubeflow/argo_workflow into scope
# via builtins.inject — no explicit import of those names is needed.
_PREAMBLE = 'load("//mlody/common/executor.mlody")\n'

_PREAMBLE_WITH_TASK = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/representation.mlody")\n'
    'load("//mlody/common/build_ref.mlody")\n'
    'load("//mlody/common/implementation.mlody")\n'
    'load("//mlody/common/values.mlody")\n'
    'load("//mlody/common/action.mlody")\n'
    'load("//mlody/common/executor.mlody")\n'
    'load("//mlody/common/task.mlody")\n'
)


def _eval(extra_mlody: str) -> Evaluator:
    """Evaluate a snippet that has access to executor kinds."""
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def _eval_with_task(extra_mlody: str) -> Evaluator:
    """Evaluate a snippet that also loads task.mlody (for TC-009/TC-010/TC-011)."""
    script = _PREAMBLE_WITH_TASK + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    return ev


def _get(ev: Evaluator, name: str) -> object:
    return ev._module_globals[ev.root_path / "test.mlody"][name]


# ---------------------------------------------------------------------------
# TC-001: host() returns kind="executor", type="host"
# ---------------------------------------------------------------------------


def test_host_returns_kind_executor() -> None:
    """TC-001: host() returns kind='executor'."""
    ev = _eval("result = host()")
    assert _get(ev, "result").kind == "executor"  # type: ignore[union-attr]


def test_host_returns_type_host() -> None:
    """TC-001: host() returns type='host'."""
    ev = _eval("result = host()")
    assert _get(ev, "result").type == "host"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-002: kubernetes() returns kind="executor", type="kubernetes"
# ---------------------------------------------------------------------------


def test_kubernetes_returns_kind_executor() -> None:
    """TC-002: kubernetes() returns kind='executor'."""
    ev = _eval("result = kubernetes()")
    assert _get(ev, "result").kind == "executor"  # type: ignore[union-attr]


def test_kubernetes_returns_type_kubernetes() -> None:
    """TC-002: kubernetes() returns type='kubernetes'."""
    ev = _eval("result = kubernetes()")
    assert _get(ev, "result").type == "kubernetes"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-003: kubernetes(namespace="prod") stores namespace correctly
# ---------------------------------------------------------------------------


def test_kubernetes_stores_namespace() -> None:
    """TC-003: kubernetes(namespace='prod') stores namespace correctly."""
    ev = _eval('result = kubernetes(namespace="prod")')
    assert _get(ev, "result").namespace == "prod"  # type: ignore[union-attr]


def test_kubernetes_stores_service_account() -> None:
    """TC-003: kubernetes(service_account='sa') stores service_account correctly."""
    ev = _eval('result = kubernetes(service_account="my-sa")')
    assert _get(ev, "result").service_account == "my-sa"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-004: kubeflow() returns kind="executor", type="kubeflow"
# ---------------------------------------------------------------------------


def test_kubeflow_returns_kind_executor() -> None:
    """TC-004: kubeflow() returns kind='executor'."""
    ev = _eval("result = kubeflow()")
    assert _get(ev, "result").kind == "executor"  # type: ignore[union-attr]


def test_kubeflow_returns_type_kubeflow() -> None:
    """TC-004: kubeflow() returns type='kubeflow'."""
    ev = _eval("result = kubeflow()")
    assert _get(ev, "result").type == "kubeflow"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-005: kubeflow(pipeline_name="train") stores pipeline_name correctly
# ---------------------------------------------------------------------------


def test_kubeflow_stores_pipeline_name() -> None:
    """TC-005: kubeflow(pipeline_name='train') stores pipeline_name correctly."""
    ev = _eval('result = kubeflow(pipeline_name="train")')
    assert _get(ev, "result").pipeline_name == "train"  # type: ignore[union-attr]


def test_kubeflow_stores_experiment() -> None:
    """TC-005: kubeflow(experiment='exp1') stores experiment correctly."""
    ev = _eval('result = kubeflow(experiment="exp1")')
    assert _get(ev, "result").experiment == "exp1"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-006: argo_workflow() returns kind="executor", type="argo_workflow"
# ---------------------------------------------------------------------------


def test_argo_workflow_returns_kind_executor() -> None:
    """TC-006: argo_workflow() returns kind='executor'."""
    ev = _eval("result = argo_workflow()")
    assert _get(ev, "result").kind == "executor"  # type: ignore[union-attr]


def test_argo_workflow_returns_type_argo_workflow() -> None:
    """TC-006: argo_workflow() returns type='argo_workflow'."""
    ev = _eval("result = argo_workflow()")
    assert _get(ev, "result").type == "argo_workflow"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-007: argo_workflow(namespace="ml", workflow_template="t") stores attrs
# ---------------------------------------------------------------------------


def test_argo_workflow_stores_namespace() -> None:
    """TC-007: argo_workflow(namespace='ml') stores namespace correctly."""
    ev = _eval('result = argo_workflow(namespace="ml")')
    assert _get(ev, "result").namespace == "ml"  # type: ignore[union-attr]


def test_argo_workflow_stores_workflow_template() -> None:
    """TC-007: argo_workflow(workflow_template='t') stores workflow_template correctly."""
    ev = _eval('result = argo_workflow(workflow_template="t")')
    assert _get(ev, "result").workflow_template == "t"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-008: kubernetes(unknown_attr="x") raises TypeError
# ---------------------------------------------------------------------------


def test_kubernetes_unknown_attr_raises_type_error() -> None:
    """TC-008: kubernetes(unknown_attr='x') raises TypeError."""
    with pytest.raises(TypeError):
        _eval('result = kubernetes(unknown_attr="x")')


def test_host_unknown_attr_raises_type_error() -> None:
    """TC-008: host(unknown_attr='x') raises TypeError."""
    with pytest.raises(TypeError):
        _eval('result = host(unknown_attr="x")')


# ---------------------------------------------------------------------------
# TC-009: task(executor=kubernetes()) stores executor struct
# ---------------------------------------------------------------------------


def test_task_with_executor_stores_executor_struct() -> None:
    """TC-009: task with executor stores the executor struct."""
    ev = _eval_with_task(
        'value(name="out", type=integer(), location=s3())\n'
        'action(\n'
        '  name="act",\n'
        '  outputs=["out"],\n'
        '  implementation=container(build=bazel(target="//x:img")),\n'
        ')\n'
        'task(\n'
        '  name="t",\n'
        '  outputs=["out"],\n'
        '  action="act",\n'
        '  executor=kubernetes(namespace="prod"),\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.executor.kind == "executor"
    assert t.executor.type == "kubernetes"
    assert t.executor.namespace == "prod"


# ---------------------------------------------------------------------------
# TC-010: task(...) without executor is valid (executor is optional)
# ---------------------------------------------------------------------------


def test_task_without_executor_is_valid() -> None:
    """TC-010: task without executor attr is valid; executor defaults to None."""
    ev = _eval_with_task(
        'value(name="out", type=integer(), location=s3())\n'
        'action(\n'
        '  name="act",\n'
        '  outputs=["out"],\n'
        '  implementation=container(build=bazel(target="//x:img")),\n'
        ')\n'
        'task(\n'
        '  name="t",\n'
        '  outputs=["out"],\n'
        '  action="act",\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.executor is None


# ---------------------------------------------------------------------------
# TC-011: task(executor="not-a-struct") raises TypeError
# ---------------------------------------------------------------------------


def test_task_with_invalid_executor_raises_type_error() -> None:
    """TC-011: task(executor=<non-string non-executor>) raises TypeError."""
    # A plain string is a valid lazy ref; use a list to trigger the type error.
    with pytest.raises(TypeError):
        _eval_with_task(
            'value(name="out", type=integer(), location=s3())\n'
            'action(\n'
            '  name="act",\n'
            '  outputs=["out"],\n'
            '  implementation=container(build=bazel(target="//x:img")),\n'
            ')\n'
            'task(\n'
            '  name="t",\n'
            '  outputs=["out"],\n'
            '  action="act",\n'
            '  executor=["not", "an", "executor"],\n'
            ')\n'
        )
