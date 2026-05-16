"""Registry-backed MlodyValue subclasses: task, action, user, value.

These values wrap Starlark Structs from the mlody registry.  None carry
rendering logic — renderers live in ``mlody.resolver.render``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mlody.resolver.values.base import MlodyValue


@dataclass(frozen=True)
class MlodyTaskValue(MlodyValue):
    """Opaque wrapper around a task registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyActionValue(MlodyValue):
    """Opaque wrapper around an action registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyUserValue(MlodyValue):
    """Opaque wrapper around a user registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyValueValue(MlodyValue):
    """Opaque wrapper around a value registry Struct."""

    struct: object
