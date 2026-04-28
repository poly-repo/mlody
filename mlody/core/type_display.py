"""Shared formatting helpers for mlody type labels in CLI output."""

from __future__ import annotations

from collections.abc import Mapping


def format_value_type_label(value_obj: object) -> str:
    """Return the display label for a value-like object's ``type`` field."""
    return format_type_label(getattr(value_obj, "type", None))


def format_type_label(type_obj: object) -> str:
    """Return a user-facing label for a mlody type struct.

    Aggregate types include structural detail:
    - ``vector[string]``
    - ``point (tuple[float, float])``
    """
    if type_obj is None:
        return "?"
    if isinstance(type_obj, str):
        return type_obj or "?"

    display_name = _primary_type_name(type_obj)
    structure = _format_aggregate_structure(type_obj)
    if structure is None:
        return display_name

    root_kind = _aggregate_root_kind(type_obj)
    if display_name != "?" and root_kind is not None and display_name != root_kind:
        if display_name != structure:
            return f"{display_name} ({structure})"
    return structure


def _format_aggregate_structure(type_obj: object) -> str | None:
    root_kind = _aggregate_root_kind(type_obj)
    attributes = _attribute_mapping(type_obj)

    if root_kind == "vector":
        element_type = attributes.get("element_type")
        if element_type is None:
            return "vector"
        return f"vector[{format_type_label(element_type)}]"

    if root_kind == "tuple":
        raw_elements = attributes.get("_element_types")
        if not isinstance(raw_elements, (list, tuple)) or not raw_elements:
            return "tuple"
        rendered_elements = ", ".join(format_type_label(element) for element in raw_elements)
        return f"tuple[{rendered_elements}]"

    return None


def _aggregate_root_kind(type_obj: object) -> str | None:
    for field_name in ("_root_kind", "type", "name"):
        value = getattr(type_obj, field_name, None)
        if value in {"vector", "tuple"}:
            return value
    return None


def _primary_type_name(type_obj: object) -> str:
    name = getattr(type_obj, "name", None)
    if isinstance(name, str) and name:
        return name

    type_name = getattr(type_obj, "type", None)
    if isinstance(type_name, str) and type_name:
        return type_name

    return "?"


def _attribute_mapping(type_obj: object) -> Mapping[str, object]:
    attributes = getattr(type_obj, "attributes", None)
    if hasattr(attributes, "as_mapping"):
        return attributes.as_mapping()
    if isinstance(attributes, Mapping):
        return attributes
    return {}
