"""Dataclass wrapper for registered ``value`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct

if TYPE_CHECKING:
    from mlody.core.dag import PortRef


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredValue(RegisteredStructBase):
    """Mirror the shape of registered value structs."""

    _KIND: ClassVar[str] = "value"

    name: str
    description: str = ""
    type: object
    location: object
    freshness: object
    unit: object | None = None
    default: object | None = None
    source: RegisteredValue | PortRef | None = None
    representation: object | None = None
    group: str | None = None
    constraint: str | None = None
    _context_attr_policies: dict[str, object] | None = None
    _entity_type: object | None = None
    _source_value: object | None = None
    _producer_task: object | None = None
    _source_range: object | None = None
    raw: object | None = None
    lineage: object | None = None
    methods: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredValue"]
