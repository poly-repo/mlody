"""WildcardStepEngine — applies a WildcardSegment during label traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlody.core.traversal_runtime import iter_children
from mlody.resolver.engine.dispatch import register_step_engine
from mlody.resolver.resolver_impl import (
    TraversalErrorPolicy,
    _collect_record_fields,
    _is_record_struct,
    _policy_miss,
    _wrap_raw,
    is_registry_backed,
)
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.registry_backed import MlodyValueValue
from mlody.resolver.values.structural import MlodyVectorValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class WildcardStepEngine:
    """Apply a WildcardSegment to a MlodyValue.

    Supported inputs (priority order):
    1. ``MlodyVectorValue`` → return all elements.
    2. ``_RawAttrValue`` whose ``value`` is a Python ``dict`` → return dict values.
    3. Registry-backed value (task/action/user/value) wrapping a record-typed
       Starlark Struct → return all declared fields.
    4. ``MlodyValueValue`` wrapping a record-typed Struct → same.

    Non-traversable roots follow the RAISE/SKIP policy.
    """

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue:
        # Case 1: vector — return all elements unchanged
        if isinstance(value, MlodyVectorValue):
            return MlodyVectorValue(elements=value.elements)

        # Case 2: dict-backed raw attribute — delegate child iteration to the
        # shared adapter so dict dispatch is not duplicated here.
        if isinstance(value, _RawAttrValue) and isinstance(value.value, dict):
            children: list[MlodyValue] = [
                _wrap_raw(child, label) for _, child in iter_children(value.value)
            ]
            return MlodyVectorValue(elements=tuple(children))

        # Case 3: record-typed Struct wrapped in a registry-backed value
        from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

        if is_registry_backed(value):
            struct_obj = value.struct  # type: ignore[union-attr]
            if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
                field_values = _collect_record_fields(struct_obj, label)
                return MlodyVectorValue(elements=tuple(field_values))

        # Case 4: MlodyValueValue wrapping a record-typed Struct
        if isinstance(value, MlodyValueValue):
            struct_obj = value.struct
            if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
                field_values = _collect_record_fields(struct_obj, label)
                return MlodyVectorValue(elements=tuple(field_values))

        # Fallback: raw record-typed Struct passed directly (e.g. from tests)
        if isinstance(value, _Struct) and _is_record_struct(value):  # type: ignore[arg-type]
            field_values = _collect_record_fields(value, label)  # type: ignore[arg-type]
            return MlodyVectorValue(elements=tuple(field_values))

        return _policy_miss(
            policy,
            label,
            (
                f"WildcardSegment cannot traverse {type(value).__name__}; "
                "expected a vector, dict-backed value, or record-typed Struct "
                f"(label: {label!r})"
            ),
        )


register_step_engine("WildcardSegment", WildcardStepEngine())
