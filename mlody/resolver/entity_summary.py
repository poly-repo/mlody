"""Helpers for concise task/action summaries used across renderers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mlody.core.type_display import format_type_label

_RESERVED_STRUCT_FIELDS = {
    "kind",
    "type",
    "name",
    "description",
    "methods",
    "_allowed_attrs",
    "_predicate",
    "_entity_type",
    "_source_range",
    "_source_value",
    "_hash",
    "raw",
    "lineage",
}


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if hasattr(value, "as_mapping"):
        return value.as_mapping()
    if isinstance(value, Mapping):
        return value
    return None


def _named_items(container: object) -> list[tuple[str, object]]:
    mapping = _as_mapping(container)
    if mapping is not None:
        return [(str(key), item) for key, item in mapping.items()]
    if isinstance(container, (list, tuple)):
        items: list[tuple[str, object]] = []
        for index, item in enumerate(container):
            name = getattr(item, "name", None)
            item_name = str(name) if name not in (None, "") else str(index)
            items.append((item_name, item))
        return items
    return []


def _public_struct_items(value: object) -> list[tuple[str, object]]:
    mapping = _as_mapping(value)
    if mapping is None:
        return []
    items: list[tuple[str, object]] = []
    for key, item in mapping.items():
        if key in _RESERVED_STRUCT_FIELDS or key.startswith("_"):
            continue
        if item is None or callable(item):
            continue
        items.append((str(key), item))
    return items


def _summary_text(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "-"
        return ", ".join(_summary_text(item) for item in value)

    mapping = _as_mapping(value)
    if mapping is not None:
        label = (
            mapping.get("name")
            or mapping.get("type")
            or mapping.get("kind")
            or "struct"
        )
        public_items = _public_struct_items(value)
        if not public_items:
            return str(label)
        details = ", ".join(
            f"{name}={_summary_text(item)}" for name, item in public_items
        )
        return f"{label}({details})"
    return str(value)


def summarize_ports(container: object) -> list[dict[str, str]]:
    ports: list[dict[str, str]] = []
    for fallback_name, item in _named_items(container):
        ports.append(
            {
                "name": str(getattr(item, "name", fallback_name)),
                "type": format_type_label(getattr(item, "type", None)),
                "description": str(getattr(item, "description", "") or ""),
            }
        )
    return ports


def summarize_attribute(
    name: str,
    value: object,
    *,
    include_details: bool = True,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {
            "name": name,
            "value": value,
            "details": [],
            "detailsText": "",
        }

    mapping = _as_mapping(value)
    display_value = str(value)
    details: list[dict[str, str]] = []
    if mapping is not None:
        display_value = str(
            mapping.get("name") or mapping.get("type") or mapping.get("kind") or value
        )
        if include_details:
            for detail_name, detail_value in _public_struct_items(value):
                details.append(
                    {
                        "name": detail_name,
                        "value": _summary_text(detail_value),
                    }
                )
    details_text = ", ".join(
        f"{detail['name']}={detail['value']}" for detail in details
    )
    return {
        "name": name,
        "value": display_value,
        "details": details,
        "detailsText": details_text,
    }


def _grouped_action_details(actions: object) -> list[dict[str, str]]:
    if not isinstance(actions, Mapping):
        return []
    details: list[dict[str, str]] = []
    for group_name, action_value in actions.items():
        details.append(
            {
                "name": str(group_name),
                "value": str(
                    getattr(action_value, "name", None)
                    or getattr(action_value, "kind", None)
                    or action_value
                ),
            }
        )
    return details



def _grouped_implementation_details(actions: object) -> list[dict[str, str]]:
    if not isinstance(actions, Mapping):
        return []
    details: list[dict[str, str]] = []
    for group_name, action_value in actions.items():
        implementation = getattr(action_value, "implementation", None)
        if implementation is None:
            continue
        details.append(
            {
                "name": str(group_name),
                "value": _summary_text(implementation),
            }
        )
    return details



def _grouped_attribute(name: str, details: list[dict[str, str]]) -> dict[str, Any] | None:
    if not details:
        return None
    return {
        "name": name,
        "value": "grouped",
        "details": details,
        "detailsText": ", ".join(
            f"{detail['name']}={detail['value']}" for detail in details
        ),
    }



def summarize_task_struct(task_struct: object) -> dict[str, Any]:
    action = getattr(task_struct, "action", None)
    attributes: list[dict[str, Any]] = []

    if isinstance(action, Mapping):
        action_summary = _grouped_attribute("action", _grouped_action_details(action))
        implementation_summary = _grouped_attribute(
            "implementation",
            _grouped_implementation_details(action),
        )
    else:
        action_summary = summarize_attribute("action", action, include_details=False)
        implementation_summary = summarize_attribute(
            "implementation",
            getattr(action, "implementation", None),
        )

    if action_summary is not None:
        attributes.append(action_summary)
    if implementation_summary is not None:
        attributes.append(implementation_summary)

    execution_summary = summarize_attribute(
        "execution",
        getattr(task_struct, "execution", None),
    )
    if execution_summary is not None:
        attributes.append(execution_summary)

    return {
        "kind": "task",
        "name": str(getattr(task_struct, "name", "?")),
        "description": str(getattr(task_struct, "description", "") or ""),
        "attributes": attributes,
        "inputs": summarize_ports(getattr(task_struct, "inputs", None)),
        "outputs": summarize_ports(getattr(task_struct, "outputs", None)),
        "config": summarize_ports(getattr(task_struct, "config", None)),
    }


def summarize_action_struct(action_struct: object) -> dict[str, Any]:
    attributes: list[dict[str, Any]] = []
    implementation_summary = summarize_attribute(
        "implementation",
        getattr(action_struct, "implementation", None),
    )
    if implementation_summary is not None:
        attributes.append(implementation_summary)

    return {
        "kind": "action",
        "name": str(getattr(action_struct, "name", "?")),
        "description": str(getattr(action_struct, "description", "") or ""),
        "attributes": attributes,
        "inputs": summarize_ports(getattr(action_struct, "inputs", None)),
        "outputs": summarize_ports(getattr(action_struct, "outputs", None)),
        "config": summarize_ports(getattr(action_struct, "config", None)),
    }
