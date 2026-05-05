"""Dataclass wrapper for registered ``location`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredLocation(RegisteredStructBase):
    """Mirror the shape of registered location descriptors."""

    _KIND: ClassVar[str] = "location"

    type: str
    name: str
    attributes: dict[str, object]
    _allowed_attrs: dict[str, object]
    validator: object
    abstract: bool
    _root_kind: str
    description: str | None = None
    _attrs_mandatory: set[str] | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredLocation"]
