"""Helpers for building and appending mlody lineage events."""

from __future__ import annotations

from dataclasses import dataclass

from mlody.common.struct import (
    is_struct_like,
    struct_like_as_mapping,
    struct_like_updated,
)

from mlody.core.place import AssignmentMode


@dataclass(slots=True)
class LineageEvent:
    """Metadata for one successful assignment."""

    accessor: str
    new_value: object
    source: str | None
    reason: str | None
    timestamp: str | None
    mode: AssignmentMode
    details: object | None = None
    kind: str = "lineage_event"


def build_lineage_event(
    *,
    accessor: str,
    new_value: object,
    source: str | None,
    reason: str | None,
    timestamp: str | None,
    mode: AssignmentMode,
    details: object | None = None,
) -> LineageEvent:
    """Build a lineage event for a successful assignment."""
    return LineageEvent(
        accessor=accessor,
        new_value=new_value,
        source=source,
        reason=reason,
        timestamp=timestamp,
        mode=mode,
        details=details,
    )


def record_lineage(
    value: object,
    event: LineageEvent,
) -> bool:
    """Append *event* in place when *value* exposes a mutable ``_lineage`` list.

    Returns ``True`` when the event was recorded and ``False`` when lineage is
    unavailable or an equal event was already present.
    """

    lineage: list[object] | None = None
    if is_struct_like(value):
        candidate = struct_like_as_mapping(value).get("_lineage")
        if isinstance(candidate, list):
            lineage = candidate
    elif isinstance(value, dict):
        candidate = value.get("_lineage")
        if isinstance(candidate, list):
            lineage = candidate

    if lineage is None:
        return False

    if event in lineage:
        return False

    lineage.append(event)
    return True


def append_lineage(
    value: object,
    event: LineageEvent,
    *,
    mode: AssignmentMode,
) -> object:
    """Append a lineage event to a value and return the updated value."""
    _ = mode
    if is_struct_like(value):
        lineage = list(struct_like_as_mapping(value).get("_lineage", []))
        lineage.append(event)
        return struct_like_updated(value, _lineage=lineage)
    if isinstance(value, dict):
        updated = dict(value)
        lineage = list(updated.get("_lineage", []))
        lineage.append(event)
        updated["_lineage"] = lineage
        return updated
    return value
