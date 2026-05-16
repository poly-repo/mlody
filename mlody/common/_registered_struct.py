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
    _REGISTERED: ClassVar[dict[str, type[RegisteredStructBase]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        kind = getattr(cls, "_KIND", None)
        # Only register concrete subclasses that declare a non-empty string _KIND.
        # This prevents abstract or test-only subclasses without _KIND from
        # polluting the registry (R-001 mitigation from design.md).
        if isinstance(kind, str) and kind:
            RegisteredStructBase._REGISTERED[kind] = cls

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
) -> dict[str, _WrappedStruct]:
    """Normalize a port collection to a name-keyed dict of wrapped structs."""

    if value is None:
        return {}
    if isinstance(value, Struct):
        return {
            name: _wrap_struct_item(item, wrapper=wrapper, field_name=field_name)
            for name, item in value.as_mapping().items()
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


# Kinds for which ``wrap_registered_struct`` wraps an evaluator-registered Struct
# into its typed Python dataclass.  Intentionally limited to user-authored entity
# kinds (roots, values, actions, tasks, users, configs).  System-level kinds
# (type, location, freshness, etc.) are excluded because their Structs may be
# used as Starlark-first-class values and must remain plain Structs.
_ENTITY_KINDS_FOR_STRUCT_WRAP: frozenset[str] = frozenset(
    {"root", "value", "action", "task", "user", "config"}
)

# Kinds for which ``wrap_method_result`` upgrades a plain Struct into its typed
# Python dataclass.  Matches the original ``_method_wrapper_for_kind`` coverage.
# ``generic`` is intentionally excluded: mm.mlody generic structs are used as
# Starlark values in method dispatch contexts and must remain plain Structs.
_ENTITY_KINDS_FOR_METHOD_WRAP: frozenset[str] = frozenset(
    {
        "root",
        "type",
        "location",
        "freshness",
        "representation",
        "value",
        "action",
        "task",
        "user",
        "build_ref",
        "implementation",
        "executor",
        "config",
    }
)


def _ensure_registered() -> None:
    """Import all RegisteredX subclasses so that __init_subclass__ has fired.

    ``RegisteredStructBase._REGISTERED`` is populated lazily: each subclass
    registers itself via ``__init_subclass__`` only when its module is first
    imported.  Callers that reach this module before any Registered* import
    would otherwise find an empty registry.  This function is a cheap guard
    (each import is a no-op after the first call) called at the start of both
    public wrap functions.
    """
    import mlody.common.action  # noqa: PLC0415, F401
    import mlody.common.build_ref  # noqa: PLC0415, F401
    import mlody.common.config  # noqa: PLC0415, F401
    import mlody.common.executor  # noqa: PLC0415, F401
    import mlody.common.freshness  # noqa: PLC0415, F401
    import mlody.common.generic  # noqa: PLC0415, F401
    import mlody.common.implementation  # noqa: PLC0415, F401
    import mlody.common.location  # noqa: PLC0415, F401
    import mlody.common.representation  # noqa: PLC0415, F401
    import mlody.common.root  # noqa: PLC0415, F401
    import mlody.common.task  # noqa: PLC0415, F401
    import mlody.common.type  # noqa: PLC0415, F401
    import mlody.common.user  # noqa: PLC0415, F401
    import mlody.common.value  # noqa: PLC0415, F401


def wrap_registered_struct(kind: str, value: object) -> object:
    """Wrap a registered evaluator Struct in its typed dataclass when possible.

    Looks up the wrapper class in ``RegisteredStructBase._REGISTERED`` for
    user-authored entity kinds only.  System-level kinds (type, location, etc.)
    are returned unchanged to preserve their raw Struct representation in the
    Starlark evaluation context.
    """
    if kind not in _ENTITY_KINDS_FOR_STRUCT_WRAP:
        return value
    _ensure_registered()
    wrapper = RegisteredStructBase._REGISTERED.get(kind)
    return _wrap_struct_with_wrapper(wrapper, value)


def wrap_method_result(value: object) -> object:
    """Wrap a method-returned Struct in its typed dataclass when possible."""
    if isinstance(value, RegisteredStructBase):
        return value
    if not isinstance(value, Struct):
        return value
    kind = getattr(value, "kind", None)
    if not isinstance(kind, str):
        return value
    if kind not in _ENTITY_KINDS_FOR_METHOD_WRAP:
        return value
    _ensure_registered()
    wrapper = RegisteredStructBase._REGISTERED.get(kind)
    return _wrap_struct_with_wrapper(wrapper, value)


def _wrap_struct_with_wrapper(
    wrapper: type[object] | None,
    value: object,
) -> object:
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


