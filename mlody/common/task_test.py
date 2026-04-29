"""Integration tests for mlody/common/task.mlody."""
from __future__ import annotations

import uuid
from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
import mlody
from mlody.core.workspace import force
from mlody.core.value_context_validation import (
    ContextRestrictedValueValidationError,
    validate_context_restricted_values_evaluator,
)

assert mlody.__name__ == "mlody"

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
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
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/representation.mlody": _REPRESENTATION_MLODY,
    "mlody/common/build_ref.mlody": _BUILD_REF_MLODY,
    "mlody/common/implementation.mlody": _IMPLEMENTATION_MLODY,
    "mlody/common/executor.mlody": _EXECUTOR_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
    "mlody/common/task.mlody": _TASK_MLODY,
}

_PREAMBLE = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/representation.mlody")\n'
    'load("//mlody/common/build_ref.mlody")\n'
    'load("//mlody/common/implementation.mlody")\n'
    'load("//mlody/common/executor.mlody")\n'
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
        validate_context_restricted_values_evaluator(ev)
    return ev


# ---------------------------------------------------------------------------
# TC-001: task() registers with kind="task" (action as direct struct)
# ---------------------------------------------------------------------------


def test_task_registers_with_kind_task() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="my_task", inputs=[], outputs=[], action="my_action")\n'
    )
    assert "my_task" in ev._tasks_by_name
    t = ev._tasks_by_name["my_task"]
    assert t.kind == "task"
    assert t.name == "my_task"


def test_task_hash_is_virtual_uuid7_and_accessible_in_mlody() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'captured = task(name="my_task", inputs=[], outputs=[], action="my_action")\n'
        'builtins.register("root", struct(name="capture", hash_value=captured._hash))\n'
    )

    task_value = ev._tasks_by_name["my_task"]
    assert getattr(task_value._hash, "kind", None) == "value"  # type: ignore[attr-defined]
    assert getattr(getattr(task_value._hash, "location", None), "type", None) == "virtual"  # type: ignore[attr-defined]

    mlody_visible_hash = ev._roots_by_name["capture"].hash_value  # type: ignore[attr-defined]
    first = force(mlody_visible_hash)
    second = force(mlody_visible_hash)

    assert first == second
    assert uuid.UUID(first).version == 7


# ---------------------------------------------------------------------------
# TC-002: action string label resolves
# ---------------------------------------------------------------------------


def test_task_action_string_label_resolves() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
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
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="t", inputs=["inp"], outputs=["out"], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.action.name == "act"
    assert t.inputs[0].name == "inp"
    assert t.outputs[0].name == "out"


# ---------------------------------------------------------------------------
# TC-004: config defaults to empty list
# ---------------------------------------------------------------------------


def test_task_config_defaults_to_empty_list() -> None:
    ev = _eval(
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="t", inputs=[], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.config == []


# ---------------------------------------------------------------------------
# TC-005: config stores value refs when provided
# ---------------------------------------------------------------------------


def test_task_config_value_refs_stored() -> None:
    ev = _eval(
        'value(name="cfg", type=integer(), location=s3())\n'
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="t", inputs=[], outputs=[], action="act", config=["cfg"])\n'
    )
    t = ev._tasks_by_name["t"]
    assert len(t.config) == 1
    assert t.config[0].name == "cfg"


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
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
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
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
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
        'action(name="a", inputs=[":x"], outputs=[], implementation=shell_script(content="dummy"))\n'
    )
    t = ev._tasks_by_name["t"]
    a = ev._actions_by_name["a"]
    assert t.action is a
    assert t.inputs[0] is ev._values_by_name["x"]


# ---------------------------------------------------------------------------
# TC-011: task and action ports are stored independently (no merging)
# ---------------------------------------------------------------------------


def test_task_and_action_ports_stored_independently() -> None:
    """TC-011: task inputs and action inputs are separate — no merging occurs."""
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[value(name="inp", location=s3())],\n'
        '  outputs=[],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[value(name="inp", type=integer(), location=posix(path="/data"))],\n'
        '    outputs=[],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    # Task's port has s3 location (type defaults to nothing since not specified)
    assert t.inputs[0].location.type == "s3"
    assert t.inputs[0].type.name == "nothing"
    # Action's port retains its own explicit type and location unchanged
    assert t.action.inputs[0].type.type == "integer"
    assert t.action.inputs[0].location.type == "posix"


# ---------------------------------------------------------------------------
# TC-012: action-scoped values registered under {action}.{port}
# ---------------------------------------------------------------------------


def test_action_scoped_registration_uses_action_name() -> None:
    """TC-012: action ports register as {action_name}.{port_name}, not {task}.{action}.{port}."""
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="out", type=string(), location=s3())],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[],\n'
        '    outputs=[value(name="out", type=integer(), location=posix(path="/x"))],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    # Task-scoped: {task}.{port}
    assert ev._values_by_name.get("t.out") is not None
    assert ev._values_by_name["t.out"].type.type == "string"
    # Action-scoped: {action}.{port} (not t.act.out)
    assert ev._values_by_name.get("act.out") is not None
    assert ev._values_by_name["act.out"].type.type == "integer"
    assert ev._values_by_name.get("t.act.out") is None


# ---------------------------------------------------------------------------
# TC-013: action referenced by string label resolves correctly
# ---------------------------------------------------------------------------


def test_task_action_string_ref_resolves_and_ports_stay_separate() -> None:
    """TC-013: action referenced by string label resolves; ports are not merged."""
    ev = _eval(
        'action(\n'
        '  name="act",\n'
        '  inputs=[value(name="inp", type=integer(), location=s3())],\n'
        '  outputs=[],\n'
        '  implementation=shell_script(content="dummy")\n'
        ')\n'
        'task(name="t", inputs=[value(name="inp")], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    # Task's port retains its own defaults (nothing/inline)
    assert t.inputs[0].type.name == "nothing"
    # Action's port (resolved from string ref) retains its explicit type
    assert t.action.inputs[0].type.type == "integer"


# ---------------------------------------------------------------------------
# TC-014: task and action with same port spec coexist without error
# ---------------------------------------------------------------------------


def test_task_and_action_with_identical_port_specs_coexist() -> None:
    """TC-014: task and action can both declare the same port — stored independently."""
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="model", type=string(), location=posix(path="/tmp/model"))],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[],\n'
        '    outputs=[value(name="model", type=string(), location=posix(path="/tmp/model"))],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.outputs[0].location.type == "posix"
    assert t.action.outputs[0].location.type == "posix"


# ---------------------------------------------------------------------------
# TC-015: task port representation is independent from action port
# ---------------------------------------------------------------------------


def test_task_port_representation_independent_from_action_port() -> None:
    """TC-015: task port carries its own representation; action port is unaffected."""
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="out", type=string(), location=s3(), representation=json())],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[],\n'
        '    outputs=[value(name="out", type=string(), location=s3())],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.outputs[0].representation is not None
    assert t.outputs[0].representation.name == "json"
    assert t.action.outputs[0].representation is None


# ---------------------------------------------------------------------------
# TC-016: task-scoped value carries representation from task port
# ---------------------------------------------------------------------------


def test_scoped_value_carries_representation_from_source() -> None:
    """TC-016: task-scoped {task}.{port} carries representation from task port declaration."""
    ev = _eval(
        'task(\n'
        '  name="mytask",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="out", type=string(), location=s3(), representation=json())],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[],\n'
        '    outputs=[value(name="out", type=string(), location=s3())],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    # Task-scoped carries representation
    scoped = ev._values_by_name.get("mytask.out")
    assert scoped is not None
    assert scoped.representation is not None
    assert scoped.representation.name == "json"
    # Action-scoped has no representation
    act_scoped = ev._values_by_name.get("act.out")
    assert act_scoped is not None
    assert act_scoped.representation is None


def test_task_output_value_accepts_group_and_scoped_value_preserves_it() -> None:
    ev = _eval(
        'task(\n'
        '  name="train",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="artifact", type=string(), location=s3(), group="bundle")],\n'
        '  action=action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy")),\n'
        ')\n'
    )
    task_value = ev._tasks_by_name["train"].outputs[0]
    scoped_value = ev._values_by_name["train.artifact"]
    assert task_value.group == "bundle"
    assert scoped_value.group == "bundle"
    assert scoped_value._context_attr_policies == {"group": ("task.outputs",)}


def test_task_output_value_preserves_unit_on_scoped_clone() -> None:
    ev = _eval(
        'task(\n'
        '  name="train",\n'
        '  inputs=[],\n'
        '  outputs=[value(name="distance", type=float(), location=inline(), unit="km")],\n'
        '  action=action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy")),\n'
        ')\n'
    )
    task_value = ev._tasks_by_name["train"].outputs[0]
    scoped_value = ev._values_by_name["train.distance"]
    assert task_value.unit is not None
    assert task_value.unit.to_string() == "km"
    assert scoped_value.unit is not None
    assert scoped_value.unit.to_string() == "km"


def test_task_input_value_with_group_raises_context_validation_error() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'task(\n'
            '  name="train",\n'
            '  inputs=[value(name="artifact", type=string(), location=s3(), group="bundle")],\n'
            '  outputs=[],\n'
            '  action=action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy")),\n'
            ')\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.inputs"
    assert violation.task_name == "train"
    assert violation.slot_path == "task.inputs[0]"


def test_task_config_value_accepts_constraint() -> None:
    ev = _eval(
        'task(\n'
        '  name="train",\n'
        '  inputs=[],\n'
        '  outputs=[],\n'
        '  config=[value(name="cfg", type=string(), location=inline(), constraint="x > 0")],\n'
        '  action=action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy")),\n'
        ')\n'
    )
    assert ev._tasks_by_name["train"].config[0].constraint == "x > 0"


def test_task_output_value_with_constraint_raises_context_validation_error() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'task(\n'
            '  name="train",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="cfg", type=string(), location=s3(), constraint="x > 0")],\n'
            '  action=action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy")),\n'
            ')\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.outputs"
    assert violation.attr_name == "constraint"


def test_task_input_value_accepts_source() -> None:
    ev = _eval(
        'value(name="upstream", type=string(), location=s3())\n'
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="train", inputs=[value(name="artifact", type=string(), location=s3(), source=":upstream")], outputs=[], action="act")\n'
    )
    assert ev._tasks_by_name["train"].inputs[0].source == ":upstream"


def test_task_config_value_accepts_source() -> None:
    ev = _eval(
        'value(name="upstream", type=string(), location=s3())\n'
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="train", inputs=[], outputs=[], config=[value(name="cfg", type=string(), location=inline(), source=":upstream")], action="act")\n'
    )
    assert ev._tasks_by_name["train"].config[0].source == ":upstream"


def test_task_output_value_with_source_raises_context_validation_error() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'value(name="upstream", type=string(), location=s3())\n'
            'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
            'task(name="train", inputs=[], outputs=[value(name="artifact", type=string(), location=s3(), source=":upstream")], action="act")\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.outputs"
    assert violation.attr_name == "source"


def test_shared_source_value_is_valid_when_used_only_in_task_inputs_and_config() -> None:
    ev = _eval(
        'value(name="upstream", type=string(), location=s3())\n'
        'value(name="artifact", type=string(), location=s3(), source=":upstream")\n'
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="train", inputs=["artifact"], outputs=[], config=["artifact"], action="act")\n'
    )
    assert ev._tasks_by_name["train"].inputs[0].source == ":upstream"
    assert ev._tasks_by_name["train"].config[0].source == ":upstream"


def test_shared_source_value_fails_when_reused_in_task_outputs() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'value(name="upstream", type=string(), location=s3())\n'
            'value(name="artifact", type=string(), location=s3(), source=":upstream")\n'
            'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
            'task(name="train", inputs=[], outputs=["artifact"], action="act")\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.outputs"
    assert violation.attr_name == "source"


def test_task_action_input_value_accepts_source() -> None:
    ev = _eval(
        'value(name="upstream", type=string(), location=s3())\n'
        'action(\n'
        '  name="act",\n'
        '  inputs=[value(name="artifact", type=string(), location=s3(), source=":upstream")],\n'
        '  outputs=[],\n'
        '  implementation=shell_script(content="dummy")\n'
        ')\n'
        'task(name="train", inputs=[], outputs=[], action="act")\n'
    )
    assert ev._tasks_by_name["train"].action.inputs[0].source == ":upstream"


def test_task_action_config_value_accepts_source() -> None:
    ev = _eval(
        'value(name="upstream", type=string(), location=s3())\n'
        'action(\n'
        '  name="act",\n'
        '  inputs=[],\n'
        '  outputs=[],\n'
        '  config=[value(name="cfg", type=string(), location=inline(), source=":upstream")],\n'
        '  implementation=shell_script(content="dummy")\n'
        ')\n'
        'task(name="train", inputs=[], outputs=[], action="act")\n'
    )
    assert ev._tasks_by_name["train"].action.config[0].source == ":upstream"


def test_task_action_output_value_with_source_raises_context_validation_error() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'value(name="upstream", type=string(), location=s3())\n'
            'action(\n'
            '  name="act",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="artifact", type=string(), location=s3(), source=":upstream")],\n'
            '  implementation=shell_script(content="dummy")\n'
            ')\n'
            'task(name="train", inputs=[], outputs=[], action="act")\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.action.outputs"
    assert violation.attr_name == "source"


def test_shared_group_value_is_valid_when_used_only_in_task_outputs() -> None:
    ev = _eval(
        'value(name="artifact", type=string(), location=s3(), group="bundle")\n'
        'action(name="act", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="train", inputs=[], outputs=["artifact"], action="act")\n'
    )
    assert ev._tasks_by_name["train"].outputs[0].group == "bundle"


def test_shared_group_value_fails_when_reused_in_allowed_and_disallowed_contexts() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'value(name="artifact", type=string(), location=s3(), group="bundle")\n'
            'action(name="act_a", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
            'action(name="act_b", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
            'task(name="producer", inputs=[], outputs=["artifact"], action="act_a")\n'
            'task(name="consumer", inputs=["artifact"], outputs=[], action="act_b")\n'
        )

    assert any(
        violation.actual_context == "task.inputs" and violation.value_name == "artifact"
        for violation in exc_info.value.violations
    )


def test_top_level_action_constraint_is_valid_via_task_action_config() -> None:
    ev = _eval(
        'action(\n'
        '  name="act",\n'
        '  inputs=[],\n'
        '  outputs=[],\n'
        '  config=[value(name="cfg", type=string(), location=inline(), constraint="x > 0")],\n'
        '  implementation=shell_script(content="dummy")\n'
        ')\n'
        'task(name="train", inputs=[], outputs=[], action="act")\n'
    )
    assert ev._tasks_by_name["train"].action.config[0].constraint == "x > 0"


def test_task_action_input_with_constraint_raises_context_validation_error() -> None:
    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _eval(
            'task(\n'
            '  name="train",\n'
            '  inputs=[],\n'
            '  outputs=[],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[value(name="cfg", type=string(), location=inline(), constraint="x > 0")],\n'
            '    outputs=[],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.action.inputs"
    assert violation.attr_name == "constraint"


def test_task_attaches_declared_entity_type() -> None:
    ev = _eval(
        'action(name="my_action", inputs=[], outputs=[], implementation=shell_script(content="dummy"))\n'
        'task(name="my_task", inputs=[], outputs=[], action="my_action")\n'
    )

    task_value = ev._tasks_by_name["my_task"]
    assert task_value._entity_type.name == "mlody-task"
