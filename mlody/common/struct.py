"""mlody-owned compatibility layer for the shared Starlarkish Struct type."""

from __future__ import annotations

from collections.abc import ItemsView
from typing import Any

from common.python.starlarkish.core.struct import Struct as Struct
from common.python.starlarkish.core.struct import struct as struct

_MISSING = object()


def _struct_get(self: Struct, name: str, default: Any = None) -> Any:
    """Return a field value with an optional default for missing names."""
    return self.as_mapping().get(name, default)


def _struct_items(self: Struct) -> ItemsView[str, Any]:
    """Expose field iteration without leaking the private backing mapping."""
    return self.as_mapping().items()


def _struct_updated(self: Struct, **changes: Any) -> Struct:
    """Return a new Struct with ``changes`` applied."""
    updated_fields = dict(self.as_mapping())
    updated_fields.update(changes)
    return Struct(**updated_fields)


class _FieldAwareMethod:
    """Descriptor that preserves field access for helper names like ``items``."""

    def __init__(self, field_name: str, func: object):
        self._field_name = field_name
        self._func = func

    def __get__(self, instance: Struct | None, owner: type[Struct]) -> object:
        if instance is None:
            return self._func
        field_value = instance.as_mapping().get(self._field_name, _MISSING)
        if field_value is not _MISSING:
            return field_value
        return self._func.__get__(instance, owner)

    def __set__(self, instance: Struct, value: object) -> None:
        raise AttributeError("Struct is immutable")


# Patch the shared Struct type in place so evaluator-produced Struct instances
# automatically expose the mlody helpers without changing upstream code.
if not hasattr(Struct, "get"):
    setattr(Struct, "get", _FieldAwareMethod("get", _struct_get))

if not hasattr(Struct, "items"):
    setattr(Struct, "items", _FieldAwareMethod("items", _struct_items))

if not hasattr(Struct, "updated"):
    setattr(Struct, "updated", _FieldAwareMethod("updated", _struct_updated))


__all__ = ["Struct", "struct"]
