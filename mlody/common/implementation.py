"""Dataclass wrapper for registered ``implementation`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredImplementation(RegisteredStructBase):
    """Mirror the shape of implementation descriptors and instances."""

    _KIND: ClassVar[str] = "implementation"

    type: str
    name: str
    _allowed_attrs: dict[str, object]
    _predicate: object | None = None
    build: object | None = None
    content: str | None = None
    file: str | None = None
    interpreter: str | None = None
    path: str | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredImplementation"]
