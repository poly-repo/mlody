"""Helpers for building and appending mlody lineage events."""

from __future__ import annotations

from mlody.common.struct import Struct, is_struct_like

from mlody.core.place import AssignmentMode


def build_lineage_event(
    *,
    accessor: str,
    new_value: object,
    author: str | None,
    reason: str | None,
    timestamp: str | None,
    mode: AssignmentMode,
) -> object:
    """Build a lineage event for a successful assignment."""
    return Struct(
        kind="lineage_event",
        accessor=accessor,
        new_value=new_value,
        author=author,
        reason=reason,
        timestamp=timestamp,
        mode=mode,
    )


def append_lineage(value: object, event: object, *, mode: AssignmentMode) -> object:
    """Append a lineage event to a value and return the updated value."""
    _ = mode
    if is_struct_like(value):
        lineage = list(value.get("_lineage", []))
        lineage.append(event)
        return value.updated(_lineage=lineage)
    if isinstance(value, dict):
        updated = dict(value)
        lineage = list(updated.get("_lineage", []))
        lineage.append(event)
        updated["_lineage"] = lineage
        return updated
    return value
