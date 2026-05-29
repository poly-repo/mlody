"""Helpers for building and appending mlody lineage events."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mlody.common.struct import (
    is_struct_like,
    struct_like_as_mapping,
    struct_like_to_struct,
)

from mlody.core.place import AssignmentMode

_DEFAULT_DB_SUFFIX = Path(".cache") / "mlody" / "mlody.sqlite"
_RUNTIME_LABELS: dict[int, str] = {}
_RUNTIME_OWNER_HASHES: dict[int, str] = {}
_RUNTIME_LABEL_HASHES: dict[str, str] = {}
_HASH_LOOKUPS_IN_PROGRESS: set[int] = set()


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


def remember_runtime_label(value: object, label: str | None) -> None:
    """Remember a stable concrete label for one runtime object when available."""
    if label:
        _RUNTIME_LABELS[id(value)] = label


def remember_value_tree_labels(value: object, label: str | None) -> None:
    """Remember concrete labels for a registry entity and its named child values."""
    if not label:
        return
    remember_runtime_label(value, label)

    kind = getattr(value, "kind", None)
    if kind not in {"task", "action"}:
        return

    for field_name in ("inputs", "outputs", "config"):
        for child_name, child in _named_children(getattr(value, field_name, None)):
            remember_value_tree_labels(child, f"{label}.{field_name}.{child_name}")

    if kind != "task":
        return

    action_value = getattr(value, "action", None)
    if getattr(action_value, "kind", None) == "action":
        remember_value_tree_labels(action_value, f"{label}.action")
        return

    if isinstance(action_value, dict):
        for group_name, child_action in action_value.items():
            if getattr(child_action, "kind", None) == "action":
                remember_value_tree_labels(child_action, f"{label}.action[{group_name}]")


def materialized_lineage(value: object) -> list[object]:
    """Return persisted lineage events for *value* from the shared SQLite DB."""
    owner_label = _runtime_label(value)
    if owner_label is None:
        return []

    from mlody.db.lineage_events import open_lineage_db, read_lineage_events  # noqa: PLC0415

    conn = None
    try:
        conn = open_lineage_db(_default_db_path())
        owner_hash = _value_hash(value, conn=conn)
        if not owner_hash:
            return []
        return read_lineage_events(
            conn,
            owner_label=owner_label,
            owner_hash=owner_hash,
        )
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def record_lineage(
    value: object,
    event: LineageEvent,
) -> bool:
    """Persist *event* for *value* in the shared SQLite lineage store."""
    return _record_lineage_db_best_effort(value, event)


def append_lineage(
    value: object,
    event: LineageEvent,
    *,
    mode: AssignmentMode,
) -> object:
    """Persist a lineage event and return *value* unchanged."""
    _ = mode
    _record_lineage_db_best_effort(value, event)
    return value


def _record_lineage_db_best_effort(value: object, event: LineageEvent) -> bool:
    owner_label = _runtime_label(value)
    if owner_label is None:
        return False

    from mlody.db.lineage_events import open_lineage_db, write_lineage_event  # noqa: PLC0415

    conn = None
    try:
        conn = open_lineage_db(_default_db_path())
        owner_hash = _value_hash(value, conn=conn, event=event)
        if not owner_hash:
            return False
        _remember_runtime_hash(value, owner_hash)
        return write_lineage_event(
            conn,
            owner_label=owner_label,
            owner_hash=owner_hash,
            event=event,
        )
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def _value_hash(
    value: object,
    *,
    conn: object,
    event: LineageEvent | None = None,
) -> str | None:
    _ = conn
    content_hash = _event_content_hash(event)
    if content_hash:
        return content_hash

    value_id = id(value)
    if value_id in _HASH_LOOKUPS_IN_PROGRESS:
        return None

    _HASH_LOOKUPS_IN_PROGRESS.add(value_id)
    try:
        resolved_value = getattr(value, "_resolved_value", None)
        if resolved_value is not None and resolved_value is not value:
            return _value_hash(resolved_value, conn=conn)

        location = getattr(value, "location", None)
        inline_data = getattr(location, "data", None)
        if inline_data is not None:
            return _structured_payload_hash(inline_data)

        source_value = getattr(value, "_source_value", None)
        local_hash = _local_location_hash(location)
        if local_hash is not None and source_value is not None:
            return local_hash

        if source_value is not None:
            return _value_hash(source_value, conn=conn)

        source_attr = getattr(value, "source", None)
        if getattr(source_attr, "kind", None) == "value":
            return _value_hash(source_attr, conn=conn)

        remembered_hash = _runtime_hash(value)
        if remembered_hash:
            return remembered_hash

        if is_struct_like(value) or isinstance(value, (dict, list, tuple)):
            return _structured_payload_hash(value)

        return None
    finally:
        _HASH_LOOKUPS_IN_PROGRESS.discard(value_id)


def _event_content_hash(event: object | None) -> str | None:
    details = getattr(event, "details", None)
    if isinstance(details, dict):
        content_hash = details.get("content_hash")
        if isinstance(content_hash, str) and content_hash:
            return content_hash
    return None


def _runtime_label(value: object) -> str | None:
    label = getattr(value, "label", None)
    if isinstance(label, str) and label:
        return label

    resolved_label = getattr(value, "_resolved_label", None)
    if isinstance(resolved_label, str) and resolved_label:
        return resolved_label

    remembered = _RUNTIME_LABELS.get(id(value))
    if remembered:
        return remembered

    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return None


def _default_db_path() -> Path:
    return Path.home() / _DEFAULT_DB_SUFFIX


def _remember_runtime_hash(value: object, owner_hash: str) -> None:
    _RUNTIME_OWNER_HASHES[id(value)] = owner_hash
    owner_label = _runtime_label(value)
    if owner_label:
        _RUNTIME_LABEL_HASHES[owner_label] = owner_hash


def _runtime_hash(value: object) -> str | None:
    remembered_hash = _RUNTIME_OWNER_HASHES.get(id(value))
    if remembered_hash:
        return remembered_hash
    owner_label = _runtime_label(value)
    if owner_label:
        return _RUNTIME_LABEL_HASHES.get(owner_label)
    return None


def _named_children(value: object) -> tuple[tuple[str, object], ...]:
    if isinstance(value, dict):
        return tuple(
            (str(name), child)
            for name, child in value.items()
        )
    if is_struct_like(value):
        return tuple(
            (str(name), child)
            for name, child in struct_like_as_mapping(value).items()
        )
    return ()


def _local_location_hash(location: object) -> str | None:
    for path_text in _location_paths(location):
        path = Path(path_text).expanduser()
        if path.exists() and path.is_file():
            return _file_content_hash(path)
    return None


def _location_paths(location: object) -> tuple[str, ...]:
    direct_path = getattr(location, "path", None)
    if isinstance(direct_path, str):
        return (direct_path,)
    if isinstance(direct_path, (list, tuple)):
        return tuple(str(part) for part in direct_path)

    attributes = getattr(location, "attributes", None)
    if isinstance(attributes, dict):
        attr_path = attributes.get("path")
        if isinstance(attr_path, str):
            return (attr_path,)
        if isinstance(attr_path, (list, tuple)):
            return tuple(str(part) for part in attr_path)
    return ()


def _file_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _structured_payload_hash(value: object) -> str:
    payload = json.dumps(
        _json_payload(struct_like_to_struct(value)),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_payload(value: object) -> object:
    if is_struct_like(value):
        return {
            str(name): _json_payload(child)
            for name, child in struct_like_as_mapping(value).items()
        }
    if isinstance(value, dict):
        return {
            str(name): _json_payload(child)
            for name, child in value.items()
        }
    if isinstance(value, list):
        return [_json_payload(child) for child in value]
    if isinstance(value, tuple):
        return [_json_payload(child) for child in value]
    return value
