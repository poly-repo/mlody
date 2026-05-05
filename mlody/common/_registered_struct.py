"""Helpers for dataclasses backed by evaluator-produced Struct values."""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import ClassVar, TypeVar

from mlody.common.struct import Struct

_WrappedStruct = TypeVar("_WrappedStruct")


class RegisteredStructBase:
    """Common behavior for typed wrappers around evaluator-owned ``Struct`` values."""

    _KIND: ClassVar[str]
    _REQUIRE_STRUCT_KIND: ClassVar[bool] = True

    @property
    def kind(self) -> str:
        # Temporary compatibility shim: callers still classify many wrapped
        # entities via ``obj.kind``. Migrate those sites to ``isinstance(...)``
        # checks, then remove this property.
        return type(self)._KIND


def populate_from_struct(instance: object, value: Struct) -> None:
    """Populate a dataclass instance from an evaluator ``Struct``.

    The struct must not expose fields that the dataclass does not declare.
    Required dataclass fields must either be present on the struct or provide
    a default/default_factory on the dataclass definition.
    """

    if not isinstance(value, Struct):
        raise TypeError(
            f"{type(instance).__name__} expects a Struct, got {type(value)!r}.",
        )

    mapping = value.as_mapping()
    dataclass_fields = tuple(fields(type(instance)))
    field_names = {field_info.name for field_info in dataclass_fields}
    allowed_compat_fields: set[str] = set()
    expected_kind = getattr(type(instance), "_KIND", None)
    require_struct_kind = getattr(type(instance), "_REQUIRE_STRUCT_KIND", True)
    if isinstance(expected_kind, str):
        actual_kind = mapping.get("kind", MISSING)
        if actual_kind is MISSING:
            if require_struct_kind:
                raise ValueError(
                    f"{type(instance).__name__} expected struct field 'kind'="
                    f"{expected_kind!r}, but it was missing.",
                )
        elif actual_kind != expected_kind:
            raise ValueError(
                f"{type(instance).__name__} expected struct kind "
                f"{expected_kind!r}, got {actual_kind!r}.",
            )
        allowed_compat_fields.add("kind")

    unexpected = sorted(
        name
        for name in mapping
        if name not in field_names and name not in allowed_compat_fields
    )
    if unexpected:
        raise ValueError(
            f"{type(instance).__name__} does not declare dataclass fields for "
            f"{unexpected!r}.",
        )

    missing_required: list[str] = []
    for field_info in dataclass_fields:
        if field_info.name in mapping:
            object.__setattr__(instance, field_info.name, mapping[field_info.name])
            continue
        if field_info.default is not MISSING:
            object.__setattr__(instance, field_info.name, field_info.default)
            continue
        if field_info.default_factory is not MISSING:
            object.__setattr__(instance, field_info.name, field_info.default_factory())
            continue
        missing_required.append(field_info.name)

    if missing_required:
        raise ValueError(
            f"{type(instance).__name__} is missing required struct fields "
            f"{missing_required!r}.",
        )


def coerce_named_struct_collection(
    value: object,
    *,
    wrapper: type[_WrappedStruct],
    field_name: str,
) -> dict[str, _WrappedStruct]:
    """Normalize a port collection to a name-keyed dict of wrapped structs."""

    if value is None:
        return {}
    if isinstance(value, Struct):
        items = value.as_mapping().items()
        return {
            name: _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            for name, item in items
        }
    if isinstance(value, dict):
        return {
            str(name): _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        wrapped: dict[str, _WrappedStruct] = {}
        for item in value:
            wrapped_item = _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            item_name = getattr(wrapped_item, "name", None)
            if not isinstance(item_name, str) or not item_name:
                raise ValueError(
                    f"{field_name} expects named items; got {item!r}.",
                )
            if item_name in wrapped:
                raise ValueError(
                    f"{field_name} contains duplicate item name {item_name!r}.",
                )
            wrapped[item_name] = wrapped_item
        return wrapped
    raise TypeError(
        f"{field_name} expects a Struct, dict, list, or tuple of Struct values; "
        f"got {type(value)!r}.",
    )


def _wrap_struct_item(
    item: object,
    *,
    wrapper: type[_WrappedStruct],
    field_name: str,
) -> _WrappedStruct:
    if isinstance(item, wrapper):
        return item
    if not isinstance(item, Struct):
        raise TypeError(
            f"{field_name} expects Struct elements, got {type(item)!r}.",
        )
    return wrapper(item)
