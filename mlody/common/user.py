"""Dataclass wrapper for registered ``user`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredUser(RegisteredStructBase):
    """Mirror the shape of registered user structs."""

    _KIND: ClassVar[str] = "user"

    name: str
    description: str
    groups: list[str]
    avatar: str | None = None
    _entity_type: object | None = None
    _source_range: object | None = None
    raw: object | None = None
    lineage: object | None = None
    methods: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredUser"]
