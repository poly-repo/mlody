"""Dataclass wrapper for registered ``build_ref`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredBuildRef(RegisteredStructBase):
    """Mirror the shape of build-ref descriptors and instances."""

    _KIND: ClassVar[str] = "build_ref"

    type: str
    name: str
    _allowed_attrs: dict[str, object]
    _predicate: object | None = None
    target: str | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredBuildRef"]
