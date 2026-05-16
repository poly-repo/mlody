"""IndexStepEngine — applies an IndexSegment during label traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlody.resolver.engine.dispatch import register_step_engine
from mlody.resolver.resolver_impl import TraversalErrorPolicy, _policy_miss
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.registry_backed import MlodyValueValue
from mlody.resolver.values.structural import MlodyVectorValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class IndexStepEngine:
    """Apply an IndexSegment to a MlodyValue.

    Supported inputs:
    - ``MlodyVectorValue``: index into the ``elements`` tuple.
    - ``_RawAttrValue`` wrapping a Python list or tuple.
    - ``MlodyValueValue`` wrapping a Python list or tuple.

    Out-of-bounds and type mismatches follow the RAISE/SKIP policy.
    """

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue:
        from mlody.core.traversal_grammar import IndexSegment  # noqa: PLC0415

        assert isinstance(segment, IndexSegment)
        idx = segment.index

        if isinstance(value, MlodyVectorValue):
            elems = value.elements
            try:
                return elems[idx]
            except IndexError:
                return _policy_miss(
                    policy,
                    label,
                    (
                        f"index {idx} is out of range for vector of length {len(elems)} "
                        f"(label: {label!r})"
                    ),
                )

        if isinstance(value, _RawAttrValue) and isinstance(value.value, (list, tuple)):
            seq = value.value
            try:
                return _RawAttrValue(value=seq[idx], label=label)
            except IndexError:
                return _policy_miss(
                    policy,
                    label,
                    (
                        f"index {idx} is out of range for sequence of length {len(seq)} "
                        f"(label: {label!r})"
                    ),
                )

        if isinstance(value, MlodyValueValue) and isinstance(
            value.struct, (list, tuple)
        ):
            seq = value.struct
            try:
                return MlodyValueValue(struct=seq[idx])
            except IndexError:
                return _policy_miss(
                    policy,
                    label,
                    (
                        f"index {idx} is out of range for sequence of length {len(seq)} "
                        f"(label: {label!r})"
                    ),
                )

        return _policy_miss(
            policy,
            label,
            (
                f"IndexSegment requires a vector value but got "
                f"{type(value).__name__} (label: {label!r})"
            ),
        )


register_step_engine("IndexSegment", IndexStepEngine())
