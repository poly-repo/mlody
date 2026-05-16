"""SliceStepEngine — applies a SliceSegment during label traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlody.resolver.engine.dispatch import register_step_engine
from mlody.resolver.resolver_impl import TraversalErrorPolicy, _policy_miss, _wrap_raw
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.registry_backed import MlodyValueValue
from mlody.resolver.values.structural import MlodyVectorValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class SliceStepEngine:
    """Apply a SliceSegment to a MlodyValue.

    Supported inputs:
    - ``MlodyVectorValue``: slice the ``elements`` tuple → new ``MlodyVectorValue``.
    - ``_RawAttrValue`` wrapping a Python list or tuple → ``MlodyVectorValue``.
    - ``MlodyValueValue`` wrapping a Python list or tuple → ``MlodyVectorValue``.

    Type mismatches follow the RAISE/SKIP policy.
    """

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue:
        from mlody.core.traversal_grammar import SliceSegment  # noqa: PLC0415

        assert isinstance(segment, SliceSegment)
        sl = slice(segment.start, segment.stop, segment.step)

        if isinstance(value, MlodyVectorValue):
            sliced = value.elements[sl]
            return MlodyVectorValue(elements=tuple(sliced))

        if isinstance(value, _RawAttrValue) and isinstance(
            value.value, (list, tuple)
        ):
            sliced_raw = value.value[sl]
            return MlodyVectorValue(
                elements=tuple(_wrap_raw(v, label) for v in sliced_raw)
            )

        if isinstance(value, MlodyValueValue) and isinstance(
            value.struct, (list, tuple)
        ):
            sliced_struct = value.struct[sl]
            return MlodyVectorValue(
                elements=tuple(MlodyValueValue(struct=v) for v in sliced_struct)
            )

        return _policy_miss(
            policy,
            label,
            (
                f"SliceSegment requires a vector or sequence value but got "
                f"{type(value).__name__} (label: {label!r})"
            ),
        )


register_step_engine("SliceSegment", SliceStepEngine())
