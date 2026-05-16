"""Segment-kind dispatch table and step() entry point for traversal engines.

Each engine module registers itself at import time by calling
``register_step_engine``.  ``dispatch.py`` itself does NOT import engine modules
— they are loaded by ``mlody.resolver.engine.__init__`` to trigger registration.

This separation avoids a circular import:
  engine modules → dispatch (for register_step_engine)
  dispatch.py    → does NOT import engine modules
  __init__.py    → imports engine modules (triggers registration)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.structural import MlodyUnresolvedValue, MlodyVectorValue
from mlody.resolver.resolver_impl import TraversalErrorPolicy, _policy_miss

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class StepEngine(Protocol):
    """Protocol satisfied by all traversal step engine classes."""

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue: ...


_ENGINES: dict[str, StepEngine] = {}


def register_step_engine(segment_kind: str, engine: StepEngine) -> None:
    """Register a StepEngine for a segment kind name.

    Called at module level by each engine module after defining its class.
    ``segment_kind`` is the class name of the segment (e.g. ``"IndexSegment"``).
    """
    _ENGINES[segment_kind] = engine


def step(
    value: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Route *segment* to its registered engine and apply it to *value*.

    Raises ``TypeError`` indirectly via ``_policy_miss`` when no engine is
    registered for the segment kind (treats it as a miss rather than a crash
    so the error-policy chain is honoured).
    """
    kind = type(segment).__name__
    engine = _ENGINES.get(kind)
    if engine is None:
        return _policy_miss(
            policy, label, f"no engine registered for segment kind {kind!r}"
        )
    return engine.apply(value, segment, policy, label)
