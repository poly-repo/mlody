"""Dataclass wrapper for registered ``config`` structs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredConfig(RegisteredStructBase):
    """Mirror the shape of registered config structs."""

    _KIND: ClassVar[str] = "config"

    name: str
    rules: dict[str, object]
    description: str = ""

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredConfig"]
