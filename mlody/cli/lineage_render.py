"""Helpers for normalizing and rendering lineage values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.table import Table
from rich.text import Text

LineageSourceKind = Literal["default", "config", "user", "task"]


@dataclass(frozen=True, slots=True)
class LineageRow:
    """Normalized lineage row for console and stage renderers."""

    source: LineageSourceKind
    value: object
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
        source = _source_kind(source_text)
        rows.append(
            LineageRow(
                source=source,
                value=_event_value(event, source_text),
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
            Text(_console_value_text(row.value), style=row_style),
        )
    return table


def _event_source_text(event: object) -> str:
    source = _mapping_or_attr(event, "source")
    if source is None:
        return ""
    return str(source)


def _event_value(event: object, source_text: str) -> object:
    extracted = _extract_value(_mapping_or_attr(event, "new_value"))
    if extracted is not None:
        return extracted

    if source_text.startswith("DEFAULT: "):
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
    if source_text.startswith("CONFIG:"):
        return "config"
    if source_text.startswith("COMMAND_LINE:") or source_text.startswith("UI:"):
        return "user"
    return "task"


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


def _console_value_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return str(value)
