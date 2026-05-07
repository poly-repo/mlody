"""Dataclass wrapper for registered ``type`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredType(RegisteredStructBase):
    """Mirror the shape of registered type descriptors."""

    _KIND: ClassVar[str] = "type"

    type: str
    name: str
    attributes: dict[str, object]
    _allowed_attrs: dict[str, object]
    validator: object
    abstract: bool | None = None
    _root_kind: str | None = None
    description: str | None = None
    canonical: object | None = None
    _canonical_for_attrs: object | None = None
    virtual_attributes: object | None = None
    _attrs_mandatory: set[str] | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredType"]
