"""Dataclass wrapper for registered ``representation`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredRepresentation(RegisteredStructBase):
    """Mirror the shape of representation descriptors and instances."""

    _KIND: ClassVar[str] = "representation"

    name: str
    attributes: dict[str, object]
    _allowed_attrs: dict[str, object]
    _attrs_mandatory: set[str]
    markup: str | None = None
    schema: object | None = None
    multifile: bool | None = None
    min_length: int | None = None
    max_length: int | None = None
    total_min_length: int | None = None
    total_max_length: int | None = None
    separator: str | None = None
    header_required: bool | None = None
    methods: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredRepresentation"]
