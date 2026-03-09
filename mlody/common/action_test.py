"""Integration tests for mlody/common/action.mlody."""
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

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
}

_PREAMBLE = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/values.mlody")\n'
    'load("//mlody/common/action.mlody")\n'
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
# TC-001: action() registers with kind="action"
# ---------------------------------------------------------------------------


def test_action_registers_with_kind_action() -> None:
    ev = _eval(
        'value(name="inp", type=integer(), location=s3())\n'
        'value(name="out", type=integer(), location=s3())\n'
        'action(name="my_action", inputs=["inp"], outputs=["out"])\n'
    )
    assert "my_action" in ev._actions_by_name
    a = ev._actions_by_name["my_action"]
    assert a.kind == "action"
    assert a.name == "my_action"


# ---------------------------------------------------------------------------
# TC-002: action stores inputs and outputs
# ---------------------------------------------------------------------------


def test_action_stores_inputs_and_outputs() -> None:
    ev = _eval(
        'value(name="inp", type=integer(), location=s3())\n'
        'value(name="out", type=string(), location=s3())\n'
        'action(name="a", inputs=["inp"], outputs=["out"])\n'
    )
    a = ev._actions_by_name["a"]
    assert a.inputs[0].name == "inp"
    assert a.outputs[0].name == "out"


# ---------------------------------------------------------------------------
# TC-003: string value label in inputs resolves
# ---------------------------------------------------------------------------


def test_action_string_value_label_resolves() -> None:
    ev = _eval(
        'value(name="my_val", type=integer(), location=s3())\n'
        'action(name="a", inputs=["my_val"], outputs=[])\n'
    )
    a = ev._actions_by_name["a"]
    assert a.inputs[0].name == "my_val"
    assert a.inputs[0].kind == "value"


# ---------------------------------------------------------------------------
# TC-004: empty inputs and outputs allowed
# ---------------------------------------------------------------------------


def test_action_empty_inputs_and_outputs_allowed() -> None:
    ev = _eval('action(name="empty", inputs=[], outputs=[])\n')
    a = ev._actions_by_name["empty"]
    assert a.inputs == []
    assert a.outputs == []


# ---------------------------------------------------------------------------
# TC-005: config defaults to empty map when omitted
# ---------------------------------------------------------------------------


def test_action_config_defaults_to_empty_map() -> None:
    ev = _eval('action(name="a", inputs=[], outputs=[])\n')
    a = ev._actions_by_name["a"]
    assert a.config == Struct()


# ---------------------------------------------------------------------------
# TC-006: config is stored when provided
# ---------------------------------------------------------------------------


def test_action_config_stored() -> None:
    ev = _eval('action(name="a", inputs=[], outputs=[], config={"lr": 0.01})\n')
    a = ev._actions_by_name["a"]
    assert a.config.lr == 0.01


# ---------------------------------------------------------------------------
# TC-007: unknown value label raises NameError
# ---------------------------------------------------------------------------


def test_action_unknown_value_label_raises_name_error() -> None:
    with pytest.raises(NameError):
        _eval('action(name="a", inputs=["nonexistent"], outputs=[])\n')


# ---------------------------------------------------------------------------
# TC-008: wrong type in inputs raises TypeError
# ---------------------------------------------------------------------------


def test_action_wrong_type_in_inputs_raises_type_error() -> None:
    with pytest.raises(TypeError):
        _eval('action(name="a", inputs=[integer()], outputs=[])\n')
