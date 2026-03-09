"""Integration tests for mlody/common/task.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.core.struct import Struct
from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()
_ACTION_MLODY = (_THIS_DIR / "action.mlody").read_text()
_TASK_MLODY = (_THIS_DIR / "task.mlody").read_text()

_BASE_FILES: dict[str, str] = {
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


def _eval(extra_mlody: str) -> Evaluator:
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    return ev


# ---------------------------------------------------------------------------
# TC-001: task() registers with kind="task" (action as direct struct)
# ---------------------------------------------------------------------------


def test_task_registers_with_kind_task() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[])\n'
        'task(name="my_task", inputs=[], outputs=[], action="my_action")\n'
    )
    assert "my_task" in ev._tasks_by_name
    t = ev._tasks_by_name["my_task"]
    assert t.kind == "task"
    assert t.name == "my_task"


# ---------------------------------------------------------------------------
# TC-002: action string label resolves
# ---------------------------------------------------------------------------


def test_task_action_string_label_resolves() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[])\n'
        'task(name="t", inputs=[], outputs=[], action="my_action")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.action.kind == "action"
    assert t.action.name == "my_action"


# ---------------------------------------------------------------------------
# TC-003: task stores action, inputs, outputs
# ---------------------------------------------------------------------------


def test_task_stores_action_inputs_outputs() -> None:
    ev = _eval(
        'value(name="inp", type=integer(), location=s3())\n'
        'value(name="out", type=string(), location=s3())\n'
        'action(name="act", inputs=[], outputs=[])\n'
        'task(name="t", inputs=["inp"], outputs=["out"], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.action.name == "act"
    assert t.inputs[0].name == "inp"
    assert t.outputs[0].name == "out"


# ---------------------------------------------------------------------------
# TC-004: config defaults to empty map
# ---------------------------------------------------------------------------


def test_task_config_defaults_to_empty_map() -> None:
    ev = _eval(
        'action(name="act", inputs=[], outputs=[])\n'
        'task(name="t", inputs=[], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.config == Struct()


# ---------------------------------------------------------------------------
# TC-005: config stored when provided
# ---------------------------------------------------------------------------


def test_task_config_stored() -> None:
    ev = _eval(
        'action(name="act", inputs=[], outputs=[])\n'
        'task(name="t", inputs=[], outputs=[], action="act", config={"epochs": 10})\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.config.epochs == 10


# ---------------------------------------------------------------------------
# TC-006: unknown action label raises NameError
# ---------------------------------------------------------------------------


def test_task_unknown_action_label_raises_name_error() -> None:
    with pytest.raises(NameError):
        _eval('task(name="t", inputs=[], outputs=[], action="nonexistent")\n')


# ---------------------------------------------------------------------------
# TC-007: wrong action type (value struct) raises TypeError
# ---------------------------------------------------------------------------


def test_task_wrong_action_type_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _eval('task(name="t", inputs=[], outputs=[], action=integer())\n')


# ---------------------------------------------------------------------------
# TC-008: string value labels in inputs resolve
# ---------------------------------------------------------------------------


def test_task_string_value_labels_in_inputs_resolve() -> None:
    ev = _eval(
        'value(name="inp", type=integer(), location=s3())\n'
        'action(name="act", inputs=[], outputs=[])\n'
        'task(name="t", inputs=["inp"], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.inputs[0].name == "inp"
    assert t.inputs[0].kind == "value"


# ---------------------------------------------------------------------------
# TC-009: empty inputs and outputs allowed
# ---------------------------------------------------------------------------


def test_task_empty_inputs_outputs_allowed() -> None:
    ev = _eval(
        'action(name="act", inputs=[], outputs=[])\n'
        'task(name="t", inputs=[], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.inputs == []
    assert t.outputs == []


# ---------------------------------------------------------------------------
# TC-010: forward reference — task declared before action resolves correctly
# ---------------------------------------------------------------------------


def test_forward_reference() -> None:
    """Task can reference an action defined after it in the same file."""
    ev = _eval(
        'value(name="x", type=integer(), location=s3())\n'
        'task(name="t", inputs=[":x"], outputs=[], action=":a")\n'
        'action(name="a", inputs=[":x"], outputs=[])\n'
    )
    t = ev._tasks_by_name["t"]
    a = ev._actions_by_name["a"]
    assert t.action is a
    assert t.inputs[0] is ev._values_by_name["x"]
