"""Dataclass wrapper for registered ``executor`` structs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mlody.common._registered_struct import RegisteredStructBase, populate_from_struct
from mlody.common.struct import Struct


@dataclass(frozen=True, slots=True, init=False)
class RegisteredExecutor(RegisteredStructBase):
    """Mirror the shape of executor descriptors and instances."""

    _KIND: ClassVar[str] = "executor"

    type: str
    name: str
    _allowed_attrs: dict[str, object]
    _predicate: object | None = None
    namespace: str | None = None
    service_account: str | None = None
    pipeline_name: str | None = None
    experiment: str | None = None
    workflow_template: str | None = None
    methods: object | None = None

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)


__all__ = ["RegisteredExecutor"]
