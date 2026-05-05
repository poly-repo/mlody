"""Validation for context-restricted value attributes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.struct import Struct, is_struct_like

from mlody.core.registry_view import RegistryView

RegistryItem = tuple[tuple[object, object, object], object]


@dataclass(frozen=True)
class ContextRestrictedValueViolation:
    """A single invalid use of a context-restricted value attribute."""

    value_name: str
    attr_name: str
    actual_context: str
    allowed_contexts: tuple[str, ...]
    task_name: str | None = None
    slot_path: str | None = None


class ContextRestrictedValueValidationError(Exception):
    """Raised when one or more context-restricted value attrs are misused."""

    def __init__(
        self,
        violations: Iterable[ContextRestrictedValueViolation],
    ) -> None:
        self.violations = tuple(violations)
        lines = "\n".join(_format_violation(violation) for violation in self.violations)
        super().__init__(
            f"{len(self.violations)} context-restricted value violation(s):\n{lines}"
        )


@dataclass(frozen=True)
class _ObservedBinding:
    value: object
    actual_context: str
    task_name: str
    slot_path: str


ValueKey = tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]


def validate_context_restricted_values_evaluator(evaluator: Evaluator) -> None:
    """Validate context-restricted value attrs in a resolved evaluator."""
    validate_context_restricted_values_registry(RegistryView(evaluator))


def validate_context_restricted_values_registry(registry: RegistryView) -> None:
    """Validate context-restricted value attrs in a resolved registry."""
    violations = collect_context_restricted_value_violations_from_entities(
        tasks=registry.task_values_snapshot().values(),
        actions=registry.action_values_snapshot().values(),
        values=registry.value_values_snapshot().values(),
    )
    if violations:
        raise ContextRestrictedValueValidationError(violations)


def validate_context_restricted_values_registry_items(
    items: Iterable[RegistryItem],
) -> None:
    """Validate context-restricted value attrs from resolved registry items."""
    violations = collect_context_restricted_value_violations(items)
    if violations:
        raise ContextRestrictedValueValidationError(violations)


def collect_context_restricted_value_violations(
    items: Iterable[RegistryItem],
) -> tuple[ContextRestrictedValueViolation, ...]:
    """Return contextual value violations for resolved registry items."""
    registry_values: list[object] = []
    tasks: list[object] = []
    actions: list[object] = []

    for _key, entity in items:
        if not is_struct_like(entity):
            continue
        entity_kind = getattr(entity, "kind", None)
        if entity_kind == "value":
            registry_values.append(entity)
        elif entity_kind == "task":
            tasks.append(entity)
        elif entity_kind == "action":
            actions.append(entity)

    return collect_context_restricted_value_violations_from_entities(
        tasks=tasks,
        actions=actions,
        values=registry_values,
    )


def collect_context_restricted_value_violations_from_entities(
    *,
    tasks: Iterable[object],
    actions: Iterable[object],
    values: Iterable[object],
) -> tuple[ContextRestrictedValueViolation, ...]:
    """Return contextual value violations for already-grouped resolved entities."""
    registry_values = [
        value
        for value in values
        if is_struct_like(value) and getattr(value, "kind", None) == "value"
    ]
    task_values = [
        task
        for task in tasks
        if is_struct_like(task) and getattr(task, "kind", None) == "task"
    ]
    action_values = [
        action
        for action in actions
        if is_struct_like(action) and getattr(action, "kind", None) == "action"
    ]

    observed_by_value_key: dict[ValueKey, list[_ObservedBinding]] = defaultdict(list)
    policy_values: dict[ValueKey, object] = {}

    for action in action_values:
        action_name = _entity_name(action)
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=action_name,
            context_name="action.inputs",
            slot_field="inputs",
            container=action,
        )
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=action_name,
            context_name="action.outputs",
            slot_field="outputs",
            container=action,
        )
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=action_name,
            context_name="action.config",
            slot_field="config",
            container=action,
        )

    for task in task_values:
        task_name = _entity_name(task)
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=task_name,
            context_name="task.inputs",
            slot_field="inputs",
            container=task,
        )
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=task_name,
            context_name="task.outputs",
            slot_field="outputs",
            container=task,
        )
        _record_bindings(
            observed_by_value_key,
            policy_values,
            task_name=task_name,
            context_name="task.config",
            slot_field="config",
            container=task,
        )

        action = getattr(task, "action", None)
        if is_struct_like(action) and getattr(action, "kind", None) == "action":
            _record_bindings(
                observed_by_value_key,
                policy_values,
                task_name=task_name,
                context_name="task.action.inputs",
                slot_field="inputs",
                container=action,
            )
            _record_bindings(
                observed_by_value_key,
                policy_values,
                task_name=task_name,
                context_name="task.action.outputs",
                slot_field="outputs",
                container=action,
            )
            _record_bindings(
                observed_by_value_key,
                policy_values,
                task_name=task_name,
                context_name="task.action.config",
                slot_field="config",
                container=action,
            )

    for value in registry_values:
        if _is_scoped_clone(value):
            continue
        policies = _context_policies(value)
        if policies:
            policy_values.setdefault(_value_key(value, policies), value)

    violations: list[ContextRestrictedValueViolation] = []
    for value_key, value in policy_values.items():
        policies = _context_policies(value)
        observed = observed_by_value_key.get(value_key, [])
        if observed:
            observed_contexts = tuple(binding.actual_context for binding in observed)
            for binding in observed:
                for attr_name, allowed_contexts in policies.items():
                    if _skip_direct_action_binding(
                        binding.actual_context,
                        allowed_contexts,
                        observed_contexts,
                    ):
                        continue
                    if binding.actual_context in allowed_contexts:
                        continue
                    violations.append(
                        ContextRestrictedValueViolation(
                            value_name=_entity_name(binding.value),
                            attr_name=attr_name,
                            actual_context=binding.actual_context,
                            allowed_contexts=allowed_contexts,
                            task_name=binding.task_name,
                            slot_path=binding.slot_path,
                        )
                    )
            continue

        for attr_name, allowed_contexts in policies.items():
            if "standalone" in allowed_contexts:
                continue
            violations.append(
                ContextRestrictedValueViolation(
                    value_name=_entity_name(value),
                    attr_name=attr_name,
                    actual_context="standalone",
                    allowed_contexts=allowed_contexts,
                )
            )

    return tuple(violations)


def _record_bindings(
    observed_by_value_key: dict[ValueKey, list[_ObservedBinding]],
    policy_values: dict[ValueKey, object],
    *,
    task_name: str,
    context_name: str,
    slot_field: str,
    container: object,
) -> None:
    for index, value in enumerate(_iter_slot_values(container, slot_field)):
        policies = _context_policies(value)
        if not policies:
            continue
        value_key = _value_key(value, policies)
        policy_values[value_key] = value
        observed_by_value_key[value_key].append(
            _ObservedBinding(
                value=value,
                actual_context=context_name,
                task_name=task_name,
                slot_path=f"{context_name}[{index}]",
            )
        )


def _iter_slot_values(container: object, field_name: str) -> tuple[object, ...]:
    raw_values = getattr(container, field_name, None)
    if isinstance(raw_values, dict):
        values = tuple(raw_values.values())
    elif is_struct_like(raw_values):
        values = tuple(raw_values.as_mapping().values())
    elif isinstance(raw_values, (list, tuple)):
        values = tuple(raw_values)
    else:
        return ()
    return tuple(
        value
        for value in values
        if is_struct_like(value) and getattr(value, "kind", None) == "value"
    )


def _context_policies(value: object) -> dict[str, tuple[str, ...]]:
    raw_policies = getattr(value, "_context_attr_policies", None)
    if raw_policies is None:
        return {}
    if is_struct_like(raw_policies):
        items = raw_policies.as_mapping().items()
    elif isinstance(raw_policies, dict):
        items = raw_policies.items()
    else:
        return {}

    policies: dict[str, tuple[str, ...]] = {}
    for attr_name, allowed_raw in items:
        if not isinstance(attr_name, str):
            continue
        if isinstance(allowed_raw, str):
            policies[attr_name] = (allowed_raw,)
            continue
        if isinstance(allowed_raw, (list, tuple)):
            allowed_contexts = tuple(
                context for context in allowed_raw if isinstance(context, str)
            )
            if allowed_contexts:
                policies[attr_name] = allowed_contexts
    return policies


def _skip_direct_action_binding(
    actual_context: str,
    allowed_contexts: tuple[str, ...],
    observed_contexts: tuple[str, ...],
) -> bool:
    if not actual_context.startswith("action."):
        return False
    if any(context.startswith("task.action.") for context in observed_contexts):
        return True
    return "standalone" not in allowed_contexts and not any(
        context.startswith("action.") for context in allowed_contexts
    )


def _is_scoped_clone(value: object) -> bool:
    name = getattr(value, "name", None)
    return isinstance(name, str) and "." in name


def _entity_name(value: object) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) and name else "<unnamed>"


def _value_key(
    value: object,
    policies: dict[str, tuple[str, ...]] | None = None,
) -> ValueKey:
    active_policies = policies if policies is not None else _context_policies(value)
    return (
        _entity_name(value),
        tuple(sorted((attr_name, allowed) for attr_name, allowed in active_policies.items())),
    )


def _format_violation(violation: ContextRestrictedValueViolation) -> str:
    owner = ""
    if violation.task_name is not None and violation.slot_path is not None:
        owner = f" [task={violation.task_name!r}, slot={violation.slot_path!r}]"
    allowed = ", ".join(repr(context) for context in violation.allowed_contexts)
    return (
        f"  value {violation.value_name!r}: attr {violation.attr_name!r} "
        f"is not allowed in {violation.actual_context!r}; allowed: ({allowed})"
        f"{owner}"
    )


__all__ = [
    "ContextRestrictedValueValidationError",
    "ContextRestrictedValueViolation",
    "collect_context_restricted_value_violations",
    "validate_context_restricted_values_evaluator",
    "validate_context_restricted_values_registry",
    "validate_context_restricted_values_registry_items",
]
