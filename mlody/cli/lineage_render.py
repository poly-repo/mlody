"""Helpers for normalizing and rendering lineage values."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from rich.table import Table
from rich.text import Text

LineageSourceKind = Literal["default", "context", "config", "user", "task"]


@dataclass(frozen=True, slots=True)
class LineageRow:
    """Normalized lineage row for console and stage renderers."""

    source: str
    value: object
    details: object | None
    active: bool


def is_lineage_type(type_struct: object) -> bool:
    """Return ``True`` when *type_struct* is a lineage-event vector."""

    type_name = getattr(type_struct, "name", None) or getattr(
        type_struct,
        "type_name",
        None,
    )
    root_kind = getattr(type_struct, "_root_kind", None)
    if type_name != "vector" and root_kind != "vector":
        return False

    attrs = getattr(type_struct, "attributes", None)
    element_type = attrs.get("element_type") if isinstance(attrs, dict) else None
    if element_type is None:
        element_type = getattr(type_struct, "element_type", None)
    element_name = getattr(element_type, "name", None) or getattr(
        element_type,
        "type_name",
        None,
    )
    if element_name is None and isinstance(element_type, str) and element_type.startswith(":"):
        element_name = element_type[1:]
    return element_name == "mlody-lineage-event"


def lineage_rows_from_payload(payload: object) -> list[LineageRow] | None:
    """Return normalized lineage rows, or ``None`` for non-lineage payloads."""

    if not isinstance(payload, (list, tuple)):
        return None

    rows: list[LineageRow] = []
    for index, event in enumerate(payload):
        source_text = _event_source_text(event)
        details = _mapping_or_attr(event, "details")
        rows.append(
            LineageRow(
                source=_display_source(source_text),
                value=_event_value(event, source_text, details),
                details=details,
                active=index == len(payload) - 1,
            )
        )
    return rows


def build_lineage_console_table(rows: list[LineageRow]) -> Table:
    """Build a Rich table for normalized lineage rows."""

    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        show_lines=False,
        expand=True,
    )
    table.add_column("source", no_wrap=True, style="bold")
    table.add_column("value")

    if not rows:
        table.add_row(Text("", style="dim"), Text("(empty)", style="dim"))
        return table

    for row in rows:
        row_style = None if row.active else "dim strike"
        table.add_row(
            Text(row.source, style=row_style),
            _console_value_text(row.value, row.details, style=row_style),
        )
    return table


def _event_source_text(event: object) -> str:
    source = _mapping_or_attr(event, "source")
    if source is None:
        return ""
    return str(source)


def _event_value(
    event: object,
    source_text: str,
    details: object | None,
) -> object:
    transfer_summary = _transfer_value_summary(source_text, details)
    if transfer_summary is not None:
        return transfer_summary

    new_value = _mapping_or_attr(event, "new_value")
    content_summary = _content_summary(new_value)
    if content_summary is not None:
        return content_summary

    extracted = _extract_value(new_value)
    if extracted is not None:
        return extracted

    if source_text.startswith("DEFAULT: "):
        return source_text.split(": ", 1)[1]
    if source_text.startswith("CONTEXT: "):
        return source_text.split(": ", 1)[1]
    if source_text.startswith("CONFIG: ") and "=" in source_text:
        return source_text.rsplit("=", 1)[1]
    if source_text.startswith("COMMAND_LINE: ") and "=" in source_text:
        return source_text.rsplit("=", 1)[1]
    if source_text.startswith("TASK: ") and ": " in source_text:
        return source_text.split(": ", 1)[1]
    return ""


def _source_kind(source_text: str) -> LineageSourceKind:
    if source_text.startswith("DEFAULT:"):
        return "default"
    if source_text.startswith("CONTEXT:"):
        return "context"
    if source_text.startswith("CONFIG:"):
        return "config"
    if source_text.startswith("COMMAND_LINE:") or source_text.startswith("UI:"):
        return "user"
    return "task"


def _display_source(source_text: str) -> str:
    if source_text == "downloaded from" or source_text == "copied from":
        return source_text
    return _source_kind(source_text)


def _transfer_value_summary(
    source_text: str,
    details: object | None,
) -> str | None:
    detail_mapping = _as_mapping(details)
    if detail_mapping is None:
        return None

    if source_text == "downloaded from":
        target = detail_mapping.get("staged_path") or detail_mapping.get("uri")
        if target is not None:
            return f"content of {target}"

    if source_text == "copied from":
        target = detail_mapping.get("destination_path") or detail_mapping.get("source_path")
        if target is not None:
            return f"content of {target}"

    return None


def _content_summary(value: object) -> str | None:
    location_text = _location_text(value)
    if location_text is None:
        return None
    return f"content of {location_text}"


def _location_text(value: object) -> str | None:
    if value is None:
        return None

    mapping = _as_mapping(value)
    if mapping is not None:
        location = mapping.get("location")
        if location is not None:
            nested = _location_text(location)
            if nested is not None:
                return nested

        path_text = _path_text(mapping.get("path"))
        if path_text is not None:
            return path_text

        attributes = mapping.get("attributes")
        if isinstance(attributes, dict):
            path_text = _path_text(attributes.get("path"))
            if path_text is not None:
                return path_text
            uri = attributes.get("uri")
            if uri is not None:
                return str(uri)

    location = getattr(value, "location", None)
    if location is not None:
        nested = _location_text(location)
        if nested is not None:
            return nested

    path_text = _path_text(getattr(value, "path", None))
    if path_text is not None:
        return path_text

    attributes = getattr(value, "attributes", None)
    if isinstance(attributes, dict):
        path_text = _path_text(attributes.get("path"))
        if path_text is not None:
            return path_text
        uri = attributes.get("uri")
        if uri is not None:
            return str(uri)

    return None


def _path_text(path_value: object) -> str | None:
    if path_value is None:
        return None
    if isinstance(path_value, (list, tuple)):
        return ", ".join(str(segment) for segment in path_value)
    return str(path_value)


def _extract_value(value: object) -> object | None:
    if value is None:
        return None

    mapping = _as_mapping(value)
    if mapping is not None:
        data = mapping.get("data")
        if data is not None:
            return data
        location = mapping.get("location")
        if location is not None:
            nested = _extract_value(location)
            if nested is not None:
                return nested

    data = getattr(value, "data", None)
    if data is not None:
        return data

    location = getattr(value, "location", None)
    if location is not None:
        nested = _extract_value(location)
        if nested is not None:
            return nested

    return value


def _mapping_or_attr(value: object, key: str) -> object | None:
    mapping = _as_mapping(value)
    if mapping is not None and key in mapping:
        return mapping[key]
    return getattr(value, key, None)


def _as_mapping(value: object) -> dict[object, object] | None:
    if isinstance(value, dict):
        return value
    as_mapping = getattr(value, "as_mapping", None)
    if callable(as_mapping):
        try:
            mapping = as_mapping()
        except TypeError:
            return None
        if isinstance(mapping, dict):
            return mapping
    return None


def _console_value_text(
    value: object,
    details: object | None,
    *,
    style: str | None,
) -> Text:
    text = Text(style=style)
    text.append(_scalar_text(value))

    for detail_line in _detail_lines(details):
        text.append("\n")
        text.append(detail_line, style=style)

    return text


def _scalar_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return str(value)


def _detail_lines(details: object | None) -> list[str]:
    if details is None:
        return []

    mapping = _as_mapping(details)
    if mapping is None:
        return [str(details)]

    lines: list[str] = []
    _collect_detail_lines(lines, mapping)
    return lines


def _collect_detail_lines(
    lines: list[str],
    mapping: dict[object, object],
    prefix: str = "",
) -> None:
    for raw_key, raw_value in mapping.items():
        key = str(raw_key)
        dotted_key = f"{prefix}.{key}" if prefix else key
        nested_mapping = _as_mapping(raw_value)
        if nested_mapping is not None:
            _collect_detail_lines(lines, nested_mapping, dotted_key)
            continue
        if isinstance(raw_value, list):
            value_text = json.dumps(raw_value, sort_keys=True)
        else:
            value_text = _scalar_text(raw_value)
        lines.append(f"{dotted_key}: {value_text}")
