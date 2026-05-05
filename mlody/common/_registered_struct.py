"""Helpers for dataclasses backed by evaluator-produced Struct values."""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any, ClassVar, TypeVar

from mlody.common.struct import Struct, struct_like_as_mapping, struct_like_updated

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

    def as_mapping(self) -> dict[str, object]:
        """Expose wrapper fields through the same API as evaluator Struct values."""
        return {
            "kind": self.kind,
            **{
                field_info.name: getattr(self, field_info.name)
                for field_info in fields(self)
                if hasattr(self, field_info.name)
            },
        }

    def get(self, name: str, default: Any = None) -> Any:
        """Return a field value with an optional default for missing names."""
        return self.as_mapping().get(name, default)

    def items(self) -> object:
        """Iterate wrapper fields without exposing internal dataclass machinery."""
        return self.as_mapping().items()

    def updated(self, **changes: object) -> object:
        """Return a shallow copy with selected dataclass fields replaced."""
        return struct_like_updated(self, **changes)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly dictionary, recursing into nested wrappers."""

        def _convert(value: object) -> object:
            if isinstance(value, Struct):
                return value.to_dict()
            if isinstance(value, RegisteredStructBase):
                return value.to_dict()
            if isinstance(value, dict):
                return {str(key): _convert(child) for key, child in value.items()}
            if isinstance(value, (list, tuple)):
                return [_convert(child) for child in value]
            return value

        return {
            "kind": self.kind,
            **{
                field_info.name: _convert(getattr(self, field_info.name))
                for field_info in fields(self)
                if hasattr(self, field_info.name)
            },
        }

    def __repr__(self) -> str:
        items = ", ".join(f"{name}={value!r}" for name, value in self.as_mapping().items())
        return f"{type(self).__name__}({items})"


def populate_from_struct(instance: object, value: object) -> None:
    """Populate a dataclass instance from an evaluator Struct-like value.

    The struct must not expose fields that the dataclass does not declare.
    Required dataclass fields must either be present on the struct or provide
    a default/default_factory on the dataclass definition.
    """

    if isinstance(value, Struct):
        mapping = value.as_mapping()
    elif isinstance(value, RegisteredStructBase):
        mapping = value.as_mapping()
    else:
        raise TypeError(
            f"{type(instance).__name__} expects a Struct-like value, got {type(value)!r}.",
        )
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
        if field_info.default is not MISSING or field_info.default_factory is not MISSING:
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
) -> Struct:
    """Normalize a port collection to a name-keyed Struct of wrapped structs."""

    if value is None:
        return Struct()
    if isinstance(value, Struct):
        items = value.as_mapping().items()
        return Struct(
            **{
            name: _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            for name, item in items
            }
        )
    if isinstance(value, dict):
        return Struct(
            **{
            str(name): _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            for name, item in value.items()
            }
        )
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
        return Struct(**wrapped)
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


def wrap_registered_struct(kind: str, value: object) -> object:
    """Wrap a registered evaluator Struct in its typed dataclass when possible."""
    wrapper = _wrapper_for_kind(kind)
    if wrapper is None:
        return value
    if isinstance(value, wrapper):
        return value
    if not isinstance(value, Struct):
        return value
    try:
        return wrapper(value)
    except (TypeError, ValueError):
        # Keep legacy or partially migrated shapes working until the remaining
        # callers are updated to construct the typed wrappers consistently.
        return value


def _wrapper_for_kind(kind: str) -> type[object] | None:
    from mlody.common.action import RegisteredAction
    from mlody.common.root import RegisteredRoot
    from mlody.common.task import RegisteredTask
    from mlody.common.value import RegisteredValue

    return {
        "root": RegisteredRoot,
        "value": RegisteredValue,
        "action": RegisteredAction,
        "task": RegisteredTask,
    }.get(kind)
