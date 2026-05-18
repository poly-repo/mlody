"""Integration tests for mlody/common/execution.mlody."""
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
_EXECUTION_MLODY = (_THIS_DIR / "execution.mlody").read_text()
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
    "mlody/common/execution.mlody": _EXECUTION_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
    "mlody/common/task.mlody": _TASK_MLODY,
}

_PREAMBLE = 'load("//mlody/common/execution.mlody")\n'

_PREAMBLE_WITH_TASK = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/representation.mlody")\n'
    'load("//mlody/common/build_ref.mlody")\n'
    'load("//mlody/common/implementation.mlody")\n'
    'load("//mlody/common/values.mlody")\n'
    'load("//mlody/common/action.mlody")\n'
    'load("//mlody/common/execution.mlody")\n'
    'load("//mlody/common/task.mlody")\n'
)


def _eval(extra_mlody: str) -> Evaluator:
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def _eval_with_task(extra_mlody: str) -> Evaluator:
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


def test_localhost_returns_kind_execution() -> None:
    ev = _eval("result = localhost()")
    assert _get(ev, "result").kind == "execution"  # type: ignore[union-attr]


def test_localhost_returns_type_localhost() -> None:
    ev = _eval("result = localhost()")
    assert _get(ev, "result").type == "localhost"  # type: ignore[union-attr]


def test_docker_returns_kind_execution() -> None:
    ev = _eval("result = docker()")
    assert _get(ev, "result").kind == "execution"  # type: ignore[union-attr]


def test_docker_returns_type_docker() -> None:
    ev = _eval("result = docker()")
    assert _get(ev, "result").type == "docker"  # type: ignore[union-attr]


def test_kubernetes_returns_kind_execution() -> None:
    ev = _eval("result = kubernetes()")
    assert _get(ev, "result").kind == "execution"  # type: ignore[union-attr]


def test_kubernetes_returns_type_kubernetes() -> None:
    ev = _eval("result = kubernetes()")
    assert _get(ev, "result").type == "kubernetes"  # type: ignore[union-attr]


def test_kubernetes_stores_namespace() -> None:
    ev = _eval('result = kubernetes(namespace="prod")')
    assert _get(ev, "result").namespace == "prod"  # type: ignore[union-attr]


def test_kubernetes_stores_service_account() -> None:
    ev = _eval('result = kubernetes(service_account="my-sa")')
    assert _get(ev, "result").service_account == "my-sa"  # type: ignore[union-attr]


def test_kubernetes_unknown_attr_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _eval('result = kubernetes(unknown_attr="x")')


def test_localhost_unknown_attr_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _eval('result = localhost(unknown_attr="x")')


def test_task_with_execution_stores_execution_struct() -> None:
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
        '  execution=kubernetes(namespace="prod"),\n'
        ')\n'
    )
    t = ev.registry.tasks.by_name["t"]
    assert t.execution.kind == "execution"
    assert t.execution.type == "kubernetes"
    assert t.execution.namespace == "prod"


def test_task_without_execution_is_valid() -> None:
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
    assert ev.registry.tasks.by_name["t"].execution is None


def test_task_with_invalid_execution_raises_type_error() -> None:
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
            '  execution=["not", "an", "execution"],\n'
            ')\n'
        )


def test_task_with_execution_string_ref_resolves() -> None:
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
        '  execution="docker",\n'
        ')\n'
    )
    t = ev.registry.tasks.by_name["t"]
    assert t.execution.kind == "execution"
    assert t.execution.type == "docker"


def test_task_with_localhost_allows_non_container_implementation() -> None:
    ev = _eval_with_task(
        'value(name="out", type=integer(), location=s3())\n'
        'action(\n'
        '  name="act",\n'
        '  outputs=["out"],\n'
        '  implementation=shell_script(content="echo hi"),\n'
        ')\n'
        'task(\n'
        '  name="t",\n'
        '  outputs=["out"],\n'
        '  action="act",\n'
        '  execution=localhost(),\n'
        ')\n'
    )
    assert ev.registry.tasks.by_name["t"].execution.type == "localhost"


@pytest.mark.parametrize("execution_expr", ['docker()', 'kubernetes()'])
def test_container_executions_require_container_implementation(
    execution_expr: str,
) -> None:
    with pytest.raises(ValueError, match="requires a container\\(\\) implementation"):
        _eval_with_task(
            'value(name="out", type=integer(), location=s3())\n'
            'action(\n'
            '  name="act",\n'
            '  outputs=["out"],\n'
            '  implementation=shell_script(content="echo hi"),\n'
            ')\n'
            'task(\n'
            '  name="t",\n'
            '  outputs=["out"],\n'
            '  action="act",\n'
            f'  execution={execution_expr},\n'
            ')\n'
        )
