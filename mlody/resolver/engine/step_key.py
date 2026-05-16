"""KeyStepEngine — applies a KeySegment during label traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlody.resolver.engine.dispatch import register_step_engine
from mlody.resolver.resolver_impl import TraversalErrorPolicy, _policy_miss, _wrap_raw
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class KeyStepEngine:
    """Apply a KeySegment to a MlodyValue.

    Supported inputs:
    - ``_RawAttrValue`` whose ``value`` is a Python ``dict``.

    Missing keys and type mismatches follow the RAISE/SKIP policy.
    """

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue:
        from mlody.core.traversal_grammar import KeySegment  # noqa: PLC0415

        assert isinstance(segment, KeySegment)
        key = segment.key

        d: object = None
        if isinstance(value, _RawAttrValue) and isinstance(value.value, dict):
            d = value.value

        if isinstance(d, dict):
            if key in d:
                return _wrap_raw(d[key], label)
            return _policy_miss(
                policy,
                label,
                f"key {key!r} not found in dict (label: {label!r})",
            )

        return _policy_miss(
            policy,
            label,
            (
                f"KeySegment requires a dict-backed value but got "
                f"{type(value).__name__} (label: {label!r})"
            ),
        )


register_step_engine("KeySegment", KeyStepEngine())
