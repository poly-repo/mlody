"""mlody-owned compatibility layer for the shared Starlarkish Struct type."""

from __future__ import annotations

from collections.abc import ItemsView, Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from common.python.starlarkish.core.struct import Struct as Struct
from common.python.starlarkish.core.struct import struct as struct

_MISSING = object()


def _is_dataclass_instance(value: object) -> bool:
    return is_dataclass(value) and not isinstance(value, type)


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


def _struct_getitem(self: Struct, key: str) -> Any:
    """Allow mapping-style access for Struct values."""
    return self.as_mapping()[key]


def _struct_iter(self: Struct) -> object:
    """Iterate over field names like a mapping."""
    return iter(self.as_mapping())


def _struct_len(self: Struct) -> int:
    """Return the number of fields stored on the Struct."""
    return len(self.as_mapping())


def _struct_contains(self: Struct, key: object) -> bool:
    """Return True when *key* names a stored field."""
    return key in self.as_mapping()


def is_struct_like(value: object) -> bool:
    """Return True for immutable record-like values used in mlody traversal."""
    return isinstance(value, Struct) or _is_dataclass_instance(value)


def struct_like_as_mapping(value: object) -> Mapping[str, Any]:
    """Return a shallow field mapping for Structs and dataclass wrappers."""
    if isinstance(value, Struct):
        return value.as_mapping()
    if _is_dataclass_instance(value):
        mapping_fn = getattr(value, "as_mapping", None)
        if callable(mapping_fn):
            return mapping_fn()
        return {
            field_info.name: getattr(value, field_info.name)
            for field_info in fields(value)
            if hasattr(value, field_info.name)
        }
    raise TypeError(f"expected Struct-like value, got {type(value).__name__}")


def struct_like_updated(value: object, **changes: Any) -> object:
    """Return a copy of a Struct-like value with ``changes`` applied."""
    if isinstance(value, Struct):
        return value.updated(**changes)
    if not _is_dataclass_instance(value):
        raise TypeError(f"expected Struct-like value, got {type(value).__name__}")

    field_infos = tuple(fields(value))
    field_names = {field_info.name for field_info in field_infos}
    unknown = sorted(name for name in changes if name not in field_names)
    if unknown:
        raise AttributeError(
            f"{type(value).__name__} does not declare dataclass fields for {unknown!r}."
        )

    clone = object.__new__(type(value))
    for field_info in field_infos:
        if field_info.name in changes:
            object.__setattr__(clone, field_info.name, changes[field_info.name])
            continue
        if hasattr(value, field_info.name):
            object.__setattr__(clone, field_info.name, getattr(value, field_info.name))
    return clone


def struct_like_to_struct(value: object) -> object:
    """Recursively convert Struct-like values into plain ``Struct`` objects."""

    def _convert(child: object) -> object:
        if isinstance(child, Struct):
            return Struct(
                **{
                    name: _convert(value)
                    for name, value in child.as_mapping().items()
                },
            )
        if _is_dataclass_instance(child):
            mapping = struct_like_as_mapping(child)
            return Struct(
                **{
                    name: _convert(value)
                    for name, value in mapping.items()
                },
            )
        if isinstance(child, dict):
            if all(isinstance(name, str) for name in child):
                return Struct(
                    **{
                        str(name): _convert(value)
                        for name, value in child.items()
                    },
                )
            return {
                key: _convert(value)
                for key, value in child.items()
            }
        if isinstance(child, list):
            return [_convert(value) for value in child]
        if isinstance(child, tuple):
            return tuple(_convert(value) for value in child)
        return child

    return _convert(value)


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

if not hasattr(Struct, "__getitem__"):
    setattr(Struct, "__getitem__", _struct_getitem)

if not hasattr(Struct, "__iter__"):
    setattr(Struct, "__iter__", _struct_iter)

if not hasattr(Struct, "__len__"):
    setattr(Struct, "__len__", _struct_len)

if not hasattr(Struct, "__contains__"):
    setattr(Struct, "__contains__", _struct_contains)


__all__ = [
    "Struct",
    "is_struct_like",
    "struct",
    "struct_like_as_mapping",
    "struct_like_to_struct",
    "struct_like_updated",
]
