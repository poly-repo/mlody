"""Helpers for typed virtual value traversal.

Virtual values are ``Struct(kind="value")`` instances whose location has
``type == "virtual"`` and a ``materializer`` callable. This module provides
shared helpers for forcing such values and for traversing declared type
attributes without dropping to raw Python data.
"""

from __future__ import annotations

import json
from typing import Callable

from mlody.common.struct import Struct, is_struct_like, struct_like_as_mapping
from mlody.core.traversal_runtime import step_named_child


_SENTINEL = object()
_STRING_TYPE = Struct(
    kind="type",
    type="string",
    name="string",
    _root_kind="string",
    attributes={},
    _allowed_attrs={},
)


def _is_virtual_value_struct(obj: object) -> bool:
    if not is_struct_like(obj):
        return False
    if getattr(obj, "kind", None) != "value":
        return False
    loc = getattr(obj, "location", None)
    return (
        loc is not None
        and getattr(loc, "type", None) == "virtual"
        and callable(getattr(loc, "materializer", None))
    )


def _force_virtual_value_struct(obj: object) -> object:
    if not _is_virtual_value_struct(obj):
        return obj
    loc = getattr(obj, "location", None)
    assert loc is not None
    materializer = getattr(loc, "materializer", None)
    if materializer is None:
        return obj
    return materializer(obj)


def _runtime_json_data(obj: object, *, _seen: set[int] | None = None) -> object:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if callable(obj) and not isinstance(obj, type):
        return "<callable>"
    if _is_virtual_value_struct(obj):
        return _runtime_json_data(_force_virtual_value_struct(obj), _seen=_seen)

    if _seen is None:
        _seen = set()

    is_container_like = is_struct_like(obj) or isinstance(obj, (dict, list, tuple, set))
    obj_id = id(obj)
    if is_container_like:
        if obj_id in _seen:
            return "<cycle>"
        _seen.add(obj_id)

    try:
        if is_struct_like(obj):
            result: dict[str, object] = {}
            for key, value in struct_like_as_mapping(obj).items():
                if key in {"raw", "_entity_type"}:
                    continue
                result[str(key)] = _runtime_json_data(value, _seen=_seen)
            return result
        if isinstance(obj, dict):
            return {
                str(key): _runtime_json_data(value, _seen=_seen)
                for key, value in obj.items()
            }
        if isinstance(obj, (list, tuple, set)):
            return [_runtime_json_data(value, _seen=_seen) for value in obj]
        return repr(obj)
    finally:
        if is_container_like:
            _seen.remove(obj_id)


def _runtime_json_blob(obj: object) -> str:
    return json.dumps(_runtime_json_data(obj), indent=2, sort_keys=True)


def _synthetic_raw_attribute() -> Struct:
    return Struct(
        kind="field",
        name="raw",
        type=_STRING_TYPE,
        materializer=_runtime_json_blob,
        mandatory=False,
    )


def is_virtual_value(value: object) -> bool:
    """Return True when *value* is a typed virtual value Struct."""
    if not is_struct_like(value):
        return False
    if getattr(value, "kind", None) != "value":
        return False
    loc = getattr(value, "location", None)
    return loc is not None and getattr(loc, "type", None) == "virtual"


def force_virtual_value(value: object) -> object:
    """Materialize a virtual value Struct; return all other inputs unchanged."""
    if not is_virtual_value(value):
        return value
    loc = getattr(value, "location", None)
    assert loc is not None
    materializer = getattr(loc, "materializer", None)
    if materializer is None:
        return value
    return materializer(value)


def step_object(obj: object, segment: str) -> object:
    """Traverse one segment on a materialized object.

    Lists are traversed by matching an element's ``name`` field.
    Everything else uses ``getattr``.
    """
    return step_named_child(obj, segment)


def is_record_type(value_type: object) -> bool:
    """Return True when the type represents a record/map-style schema."""
    return (
        getattr(value_type, "kind", None) == "record"
        or getattr(value_type, "_root_kind", None) == "record"
    )


def lookup_record_field(value_type: object, segment: str) -> object | None:
    """Return the declared record field spec for ``segment``, if any."""
    direct_fields = getattr(value_type, "fields", None)
    attrs = getattr(value_type, "attributes", None)
    attrs_fields = attrs.get("fields") if isinstance(attrs, dict) else None
    for field_obj in list(direct_fields or attrs_fields or []):
        if getattr(field_obj, "name", None) == segment:
            return field_obj
    return None


def _iter_legacy_virtual_attributes(value_type: object) -> tuple[object, ...]:
    attrs = getattr(value_type, "attributes", None)
    direct = getattr(value_type, "virtual_attributes", None)
    attrs_virtual = attrs.get("virtual_attributes") if isinstance(attrs, dict) else None
    return tuple(direct or attrs_virtual or ())


def lookup_virtual_attribute(value_type: object, segment: str) -> object | None:
    """Return the legacy declared virtual attribute spec for ``segment``, if any."""
    for attr_obj in _iter_legacy_virtual_attributes(value_type):
        if getattr(attr_obj, "name", None) == segment:
            return attr_obj
    return None


def lookup_declared_attribute(value_type: object, segment: str) -> object | None:
    """Return the declared child spec for ``segment``."""
    if value_type is None:
        return None
    record_field = lookup_record_field(value_type, segment)
    if record_field is not None:
        return record_field
    virtual_attr = lookup_virtual_attribute(value_type, segment)
    if virtual_attr is not None:
        return virtual_attr
    return None


def iter_declared_attributes(value_type: object) -> tuple[object, ...]:
    """Return declared child specs in deterministic traversal order."""
    attrs: list[object] = []
    seen: set[str] = set()

    if value_type is None:
        return ()

    direct_fields = getattr(value_type, "fields", None)
    type_attrs = getattr(value_type, "attributes", None)
    attrs_fields = type_attrs.get("fields") if isinstance(type_attrs, dict) else None
    for field_obj in list(direct_fields or attrs_fields or []):
        name = getattr(field_obj, "name", None)
        if isinstance(name, str) and name not in seen:
            attrs.append(field_obj)
            seen.add(name)

    for attr_obj in _iter_legacy_virtual_attributes(value_type):
        name = getattr(attr_obj, "name", None)
        if isinstance(name, str) and name not in seen:
            attrs.append(attr_obj)
            seen.add(name)

    return tuple(attrs)


def lookup_runtime_attribute(value: object, segment: str) -> object | None:
    """Return the declared child spec for a concrete runtime object.

    ``value`` may be a semantic ``value`` entity, a metadata record carrying an
    ``_entity_type`` descriptor, or a virtual value wrapper. Semantic payload
    fields on ``kind="value"`` win over framework-owned metadata attributes.
    """
    if is_virtual_value(value):
        return lookup_declared_attribute(getattr(value, "type", None), segment)

    if getattr(value, "kind", None) == "value":
        semantic_type = getattr(value, "type", None)
        semantic_field = lookup_declared_attribute(semantic_type, segment)
        if semantic_field is not None:
            return semantic_field

    entity_type = getattr(value, "_entity_type", None)
    if entity_type is not None:
        entity_attr = lookup_declared_attribute(entity_type, segment)
        if entity_attr is not None:
            return entity_attr

    if segment == "raw" and is_struct_like(value):
        return _synthetic_raw_attribute()

    return None


def make_virtual_value(
    *,
    value_type: object,
    label: str,
    materializer: Callable[[object], object],
    name: str | None = None,
) -> Struct:
    """Construct a typed virtual value Struct."""
    virtual_loc = Struct(
        kind="location",
        type="virtual",
        name="virtual",
        materializer=materializer,
    )
    fields: dict[str, object] = {
        "kind": "value",
        "type": value_type,
        "location": virtual_loc,
        "label": label,
        "_lineage": [],
    }
    if name is not None:
        fields["name"] = name
    return Struct(**fields)


def _child_label(value: Struct, segment: str) -> str:
    parent_label = getattr(value, "label", None)
    if isinstance(parent_label, str) and parent_label != "":
        return f"{parent_label}.{segment}"
    return segment


def _runtime_child_label(value: object, segment: str, label: str | None = None) -> str:
    if isinstance(label, str) and label != "":
        return label
    parent_label = getattr(value, "label", None)
    if isinstance(parent_label, str) and parent_label != "":
        return f"{parent_label}.{segment}"
    resolved_label = getattr(value, "_resolved_label", None)
    if isinstance(resolved_label, str) and resolved_label != "":
        return f"{resolved_label}.{segment}"
    owner_name = getattr(value, "name", None)
    if isinstance(owner_name, str) and owner_name != "":
        return f"{owner_name}.{segment}"
    return segment


def _declared_child_materializer(
    parent: object,
    segment: str,
    attr_spec: object,
) -> Callable[[object], object]:
    declared_materializer = getattr(attr_spec, "materializer", None)
    if callable(declared_materializer):
        cached_value: dict[str, object] = {"value": _SENTINEL}

        def _materializer(_v: object) -> object:
            existing = cached_value["value"]
            if existing is not _SENTINEL:
                return existing
            parent_value = force_virtual_value(parent)
            cached_value["value"] = declared_materializer(parent_value)
            return cached_value["value"]

        return _materializer

    def _materializer(_v: object) -> object:
        parent_value = force_virtual_value(parent)
        return step_object(parent_value, segment)

    return _materializer


def synthesize_runtime_child(
    value: object,
    segment: str,
    *,
    label: str | None = None,
) -> Struct | None:
    """Build a typed virtual child from a declared runtime field when needed."""
    attr_spec = lookup_runtime_attribute(value, segment)
    if attr_spec is None:
        return None
    declared_materializer = getattr(attr_spec, "materializer", None)
    if not callable(declared_materializer):
        if is_struct_like(value) or not hasattr(value, segment):
            return None
    child_type = getattr(attr_spec, "type", _SENTINEL)
    if child_type is _SENTINEL or child_type is None:
        return None
    return make_virtual_value(
        value_type=child_type,
        label=_runtime_child_label(value, segment, label),
        materializer=_declared_child_materializer(value, segment, attr_spec),
        name=getattr(attr_spec, "name", segment),
    )


def step_virtual_value(value: Struct, segment: str) -> Struct:
    """Traverse one declared segment on a virtual value."""
    if not is_virtual_value(value):
        raise TypeError(f"expected virtual value Struct, got {type(value).__name__}")
    return traverse_virtual_value(value, (segment,), _child_label(value, segment))


def iter_virtual_children(value: object) -> tuple[tuple[str, Struct], ...]:
    """Return the declared virtual children of *value* as typed child values."""
    if not is_virtual_value(value):
        return ()

    children: list[tuple[str, Struct]] = []
    for attr_obj in iter_declared_attributes(getattr(value, "type", None)):
        name = getattr(attr_obj, "name", None)
        if isinstance(name, str):
            children.append((name, step_virtual_value(value, name)))
    return tuple(children)


def traverse_virtual_value(value: Struct, path: tuple[str, ...], label: str) -> Struct:
    """Traverse declared attributes on a virtual value, returning a child value."""
    current = value
    for segment in path:
        current_type = getattr(current, "type", None)
        attr_spec = lookup_declared_attribute(current_type, segment)
        if attr_spec is None:
            raise AttributeError(segment)
        child_type = getattr(attr_spec, "type", _SENTINEL)
        if child_type is _SENTINEL or child_type is None:
            raise AttributeError(segment)

        current = make_virtual_value(
            value_type=child_type,
            label=label,
            materializer=_declared_child_materializer(current, segment, attr_spec),
            name=getattr(attr_spec, "name", segment),
        )
    return current
