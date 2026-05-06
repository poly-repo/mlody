"""Dataclass wrapper for registered ``root`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredRoot(RegisteredStructBase):
    """Mirror the shape of registered roots from ``mlody/core/builtins.mlody``."""

    _KIND: ClassVar[str] = "root"
    _REQUIRE_STRUCT_KIND: ClassVar[bool] = False

    name: str
    path: str
    description: str
    _entity_type: object | None = None
    _source_range: object | None = None
    raw: object | None = None
    lineage: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredRoot"]
