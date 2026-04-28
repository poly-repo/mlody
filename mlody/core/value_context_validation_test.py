"""Tests for context-restricted value-attribute validation."""

from __future__ import annotations

import pytest

from common.python.starlarkish.core.struct import Struct

from mlody.core.value_context_validation import (
    ContextRestrictedValueValidationError,
    validate_context_restricted_values_registry_items,
)


def _value(
    name: str,
    *,
    group: str | None = None,
    constraint: str | None = None,
    source: str | None = None,
) -> Struct:
    fields: dict[str, object] = {
        "kind": "value",
        "name": name,
        "location": Struct(kind="location", type="inline", name="inline"),
        "_lineage": [],
    }
    policies: dict[str, tuple[str, ...]] = {}
    if group is not None:
        fields["group"] = group
        policies["group"] = ("task.outputs",)
    if constraint is not None:
        fields["constraint"] = constraint
        policies["constraint"] = ("task.config", "task.action.config")
    if source is not None:
        fields["source"] = source
        policies["source"] = (
            "standalone",
            "task.inputs",
            "task.config",
            "task.action.inputs",
            "task.action.config",
        )
    if policies:
        fields["_context_attr_policies"] = policies
    return Struct(**fields)


def _task(
    name: str,
    *,
    inputs: list[Struct] | None = None,
    outputs: list[Struct] | None = None,
    config: list[Struct] | None = None,
    action: Struct | None = None,
) -> Struct:
    return Struct(
        kind="task",
        name=name,
        inputs=inputs or [],
        outputs=outputs or [],
        config=config or [],
        action=action,
    )


def _action(
    name: str,
    *,
    inputs: list[Struct] | None = None,
    outputs: list[Struct] | None = None,
    config: list[Struct] | None = None,
) -> Struct:
    return Struct(
        kind="action",
        name=name,
        inputs=inputs or [],
        outputs=outputs or [],
        config=config or [],
    )


def _items(*entities: Struct) -> tuple[tuple[tuple[object, object, object], object], ...]:
    items: list[tuple[tuple[object, object, object], object]] = []
    for entity in entities:
        items.append(((entity.kind, "pkg", entity.name), entity))
    return tuple(items)


def test_standalone_contextual_value_raises_standalone_violation() -> None:
    value = _value("artifact", group="bundle")

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value))

    assert exc_info.value.violations == (
        exc_info.value.violations[0],
    )
    violation = exc_info.value.violations[0]
    assert violation.value_name == "artifact"
    assert violation.attr_name == "group"
    assert violation.actual_context == "standalone"
    assert violation.allowed_contexts == ("task.outputs",)


def test_task_output_context_accepts_group() -> None:
    value = _value("artifact", group="bundle")
    task = _task("train", outputs=[value], action=_action("act"))

    validate_context_restricted_values_registry_items(_items(value, task))


def test_task_input_context_rejects_group() -> None:
    value = _value("artifact", group="bundle")
    task = _task("train", inputs=[value], action=_action("act"))

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, task))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.inputs"
    assert violation.task_name == "train"
    assert violation.slot_path == "task.inputs[0]"


def test_standalone_source_value_is_allowed() -> None:
    value = _value("artifact", source=":upstream")

    validate_context_restricted_values_registry_items(_items(value))


def test_task_input_context_accepts_source() -> None:
    value = _value("artifact", source=":upstream")
    task = _task("train", inputs=[value], action=_action("act"))

    validate_context_restricted_values_registry_items(_items(value, task))


def test_task_output_context_rejects_source() -> None:
    value = _value("artifact", source=":upstream")
    task = _task("train", outputs=[value], action=_action("act"))

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, task))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.outputs"
    assert violation.attr_name == "source"


def test_direct_action_input_rejects_source() -> None:
    value = _value("artifact", source=":upstream")
    action = _action("templated", inputs=[value])

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, action))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "action.inputs"
    assert violation.attr_name == "source"


def test_direct_action_config_rejects_source() -> None:
    value = _value("cfg", source=":upstream")
    action = _action("templated", config=[value])

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, action))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "action.config"
    assert violation.attr_name == "source"


def test_direct_action_output_rejects_source() -> None:
    value = _value("artifact", source=":upstream")
    action = _action("templated", outputs=[value])

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, action))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "action.outputs"
    assert violation.attr_name == "source"


def test_task_action_input_accepts_source_from_top_level_action() -> None:
    value = _value("artifact", source=":upstream")
    action = _action("templated", inputs=[value])
    task = _task("train", action=action)

    validate_context_restricted_values_registry_items(_items(value, action, task))


def test_task_action_config_accepts_source_from_top_level_action() -> None:
    value = _value("cfg", source=":upstream")
    action = _action("templated", config=[value])
    task = _task("train", action=action)

    validate_context_restricted_values_registry_items(_items(value, action, task))


def test_task_action_output_rejects_source_from_top_level_action() -> None:
    value = _value("artifact", source=":upstream")
    action = _action("templated", outputs=[value])
    task = _task("train", action=action)

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(_items(value, action, task))

    violation = exc_info.value.violations[0]
    assert violation.actual_context == "task.action.outputs"
    assert violation.attr_name == "source"


def test_top_level_action_template_is_allowed_without_task_materialization() -> None:
    value = _value("cfg", constraint="x > 0")
    action = _action("templated", config=[value])

    validate_context_restricted_values_registry_items(_items(value, action))


def test_task_action_config_accepts_constraint_from_top_level_action() -> None:
    value = _value("cfg", constraint="x > 0")
    action = _action("templated", config=[value])
    task = _task("train", action=action)

    validate_context_restricted_values_registry_items(_items(value, action, task))


def test_mixed_reuse_reports_disallowed_binding() -> None:
    value = _value("artifact", group="bundle")
    producer = _task("producer", outputs=[value], action=_action("build"))
    consumer = _task("consumer", inputs=[value], action=_action("use"))

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        validate_context_restricted_values_registry_items(
            _items(value, producer, consumer)
        )

    assert any(
        violation.actual_context == "task.inputs" and violation.value_name == "artifact"
        for violation in exc_info.value.violations
    )


def test_scoped_clone_values_are_ignored_for_standalone_checks() -> None:
    original = _value("artifact", group="bundle")
    scoped_fields = dict(original.as_mapping())
    scoped_fields["name"] = "train.artifact"
    scoped = Struct(**scoped_fields)
    task = _task("train", outputs=[original], action=_action("act"))

    validate_context_restricted_values_registry_items(_items(original, scoped, task))
