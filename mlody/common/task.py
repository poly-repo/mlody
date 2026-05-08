"""Dataclass wrapper for registered ``task`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import (
    RegisteredStructBase,
    coerce_named_struct_collection,
    populate_from_struct,
)
from mlody.common.struct import Struct
from mlody.common.value import RegisteredValue


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredTask(RegisteredStructBase):
    """Mirror the shape of registered task structs."""

    _KIND: ClassVar[str] = "task"

    name: str
    inputs: dict[str, RegisteredValue]
    outputs: dict[str, RegisteredValue]
    action: object
    config: dict[str, RegisteredValue]
    executor: object | None = None
    _entity_type: object | None = None
    _source_range: object | None = None
    raw: object | None = None
    lineage: object | None = None
    _hash: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)
        object.__setattr__(
            self,
            "inputs",
            coerce_named_struct_collection(
                self.inputs,
                wrapper=RegisteredValue,
                field_name="RegisteredTask.inputs",
            ),
        )
        object.__setattr__(
            self,
            "outputs",
            coerce_named_struct_collection(
                self.outputs,
                wrapper=RegisteredValue,
                field_name="RegisteredTask.outputs",
            ),
        )
        object.__setattr__(
            self,
            "config",
            coerce_named_struct_collection(
                self.config,
                wrapper=RegisteredValue,
                field_name="RegisteredTask.config",
            ),
        )


__all__ = ["RegisteredTask"]
