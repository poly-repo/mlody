"""Dataclass wrapper for registered ``generic`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredGeneric(RegisteredStructBase):
    """Mirror the shape of generics registered from ``mm.mlody``."""

    _KIND: ClassVar[str] = "generic"

    name: str
    description: str

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredGeneric"]
