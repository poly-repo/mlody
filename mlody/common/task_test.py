"""Integration tests for mlody/common/task.mlody."""
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
# TC-011: task/action input values are unified bidirectionally
# ---------------------------------------------------------------------------


def test_task_action_input_value_fields_are_unified_both_ways() -> None:
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[struct(kind="value", name="inp", location=s3())],\n'
        '  outputs=[],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[struct(kind="value", name="inp", type=integer())],\n'
        '    outputs=[],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.inputs[0].type.kind == "type"
    assert t.inputs[0].location.kind == "location"
    # Two-way unification: task-side field also fills action-side field.
    assert t.action.inputs[0].type.kind == "type"
    assert t.action.inputs[0].location.kind == "location"


# ---------------------------------------------------------------------------
# TC-012: task/action output values are unified bidirectionally
# ---------------------------------------------------------------------------


def test_task_action_output_value_fields_are_unified_both_ways() -> None:
    ev = _eval(
        'task(\n'
        '  name="t",\n'
        '  inputs=[],\n'
        '  outputs=[struct(kind="value", name="out", type=string())],\n'
        '  action=action(\n'
        '    name="act",\n'
        '    inputs=[],\n'
        '    outputs=[struct(kind="value", name="out", location=s3())],\n'
        '    implementation=shell_script(content="dummy")\n'
        '  )\n'
        ')\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.outputs[0].type.kind == "type"
    assert t.outputs[0].location.kind == "location"
    assert t.action.outputs[0].type.kind == "type"
    assert t.action.outputs[0].location.kind == "location"


# ---------------------------------------------------------------------------
# TC-013: if both sides omit required value fields, task() raises ValueError
# ---------------------------------------------------------------------------


def test_task_value_missing_required_fields_on_both_sides_raises_value_error() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        _eval(
            'task(\n'
            '  name="t",\n'
            '  inputs=[struct(kind="value", name="inp")],\n'
            '  outputs=[],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[struct(kind="value", name="inp")],\n'
            '    outputs=[],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )


# ---------------------------------------------------------------------------
# TC-014: unification also works when action is referenced by string label
# ---------------------------------------------------------------------------


def test_task_action_string_ref_value_fields_are_unified() -> None:
    ev = _eval(
        'action(\n'
        '  name="act",\n'
        '  inputs=[struct(kind="value", name="inp", type=integer())],\n'
        '  outputs=[],\n'
        '  implementation=shell_script(content="dummy")\n'
        ')\n'
        'task(name="t", inputs=[struct(kind="value", name="inp", location=s3())], outputs=[], action="act")\n'
    )
    t = ev._tasks_by_name["t"]
    assert t.inputs[0].type.kind == "type"
    assert t.inputs[0].location.kind == "location"


# ---------------------------------------------------------------------------
# TC-015: semantically equal locations across task/action do not conflict
# ---------------------------------------------------------------------------


def test_task_action_equal_location_specs_do_not_conflict() -> None:
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
    assert t.outputs[0].location.kind == "location"
    assert t.outputs[0].location.type == "posix"


# ---------------------------------------------------------------------------
# TC-016 (5.4): task port with representation=json() survives _merge_value_structs
# ---------------------------------------------------------------------------


def test_task_port_with_representation_survives_merge_task_has_json_action_has_none() -> None:
    """5.4: task port has representation=json(), action port has representation=None
    → merged value has representation.name == 'json'.
    """
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


# ---------------------------------------------------------------------------
# TC-017 (5.5): conflicting representations on task vs action raise ValueError
# ---------------------------------------------------------------------------


def test_task_conflicting_representations_raise_value_error() -> None:
    """5.5: task port has representation=json(), action port has a different
    non-None representation → ValueError naming field 'representation'.
    """
    # We create a second representation to have a distinct one for the conflict.
    # Since the spec only defines json(), we test conflict by using two json() structs
    # that are identical (no conflict) vs. a raw struct with a different name.
    # We inject a fake "csv" representation directly as a struct literal for the conflict.
    with pytest.raises(ValueError, match="representation"):
        _eval(
            'task(\n'
            '  name="t",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="out", type=string(), location=s3(), representation=json())],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[],\n'
            '    outputs=[struct(kind="value", name="out", type=string(), location=s3(),'
            '            representation=struct(kind="representation", name="csv"))],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )


def test_task_conflicting_text_representation_markup_raises_value_error() -> None:
    with pytest.raises(ValueError, match="representation"):
        _eval(
            'task(\n'
            '  name="t",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="out", type=string(), location=s3(), representation=text(markup="markdown"))],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[],\n'
            '    outputs=[value(name="out", type=string(), location=s3(), representation=text(markup="orgmode"))],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )


def test_task_conflicting_parquet_representation_schema_raises_value_error() -> None:
    with pytest.raises(ValueError, match="representation"):
        _eval(
            'typedef(name="schema_a", base=record(fields=[field(name="id", type=integer())]))\n'
            'typedef(name="schema_b", base=record(fields=[field(name="name", type=string())]))\n'
            'task(\n'
            '  name="t",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="out", type=string(), location=s3(), representation=parquet(schema=schema_a()))],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[],\n'
            '    outputs=[value(name="out", type=string(), location=s3(), representation=parquet(schema=schema_b()))],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )


def test_task_conflicting_csv_representation_separator_raises_value_error() -> None:
    with pytest.raises(ValueError, match="representation"):
        _eval(
            'task(\n'
            '  name="t",\n'
            '  inputs=[],\n'
            '  outputs=[value(name="out", type=string(), location=s3(), representation=csv(separator=","))],\n'
            '  action=action(\n'
            '    name="act",\n'
            '    inputs=[],\n'
            '    outputs=[value(name="out", type=string(), location=s3(), representation=csv(separator="|"))],\n'
            '    implementation=shell_script(content="dummy")\n'
            '  )\n'
            ')\n'
        )


# ---------------------------------------------------------------------------
# TC-018 (5.6): scoped value registered by _register_scoped_value carries representation
# ---------------------------------------------------------------------------


def test_scoped_value_carries_representation_from_source() -> None:
    """5.6: task port value with representation=json() → scoped registration
    has representation.name == 'json'.
    """
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
    scoped = ev._values_by_name.get("mytask.out")
    assert scoped is not None
    assert scoped.representation is not None
    assert scoped.representation.name == "json"
