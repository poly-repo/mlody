"""Helpers for presenting generic asset-backed values."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from mlody.core.assets import HttpAssetError, LocalAssetError, asset_from_value

_ASSET_FIELD_ORDER = (
    "origin",
    "representation",
    "freshness",
    "uri",
    "resolvedUrl",
    "path",
    "contentHash",
    "digest",
    "digestType",
    "etag",
    "lastModified",
    "length",
    "updateTime",
    "fetchedAt",
    "transport",
)


def asset_metadata_payload(value_struct: object) -> dict[str, object] | None:
    """Return normalized metadata for asset-backed non-tabular values."""

    asset = asset_from_value(value_struct)
    if asset is None:
        return None

    try:
        materialized = asset.materialize()
    except (FileNotFoundError, HttpAssetError, LocalAssetError, OSError, TypeError, ValueError):
        return None

    metadata = materialized.metadata
    payload = {
        "kind": "asset",
        "name": _string_or_none(getattr(value_struct, "name", None)),
        "origin": _asset_origin(value_struct),
        "representation": _representation_name(getattr(value_struct, "representation", None)),
        "freshness": _freshness_text(getattr(value_struct, "freshness", None)),
        "uri": metadata.uri,
        "resolvedUrl": metadata.resolved_url,
        "path": str(materialized.path),
        "contentHash": materialized.content_hash,
        "digest": metadata.digest,
        "digestType": metadata.digest_type,
        "etag": metadata.etag,
        "lastModified": metadata.last_modified,
        "length": metadata.length,
        "updateTime": metadata.update_time,
        "fetchedAt": metadata.fetched_at,
        "transport": metadata.transport,
    }
    return {key: value for key, value in payload.items() if value is not None}


def build_asset_metadata_console_table(payload: dict[str, object]) -> Table:
    """Build a simple Rich table for asset metadata."""

    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        show_lines=False,
        expand=True,
    )
    table.add_column("field", no_wrap=True, style="bold")
    table.add_column("value")

    rows_added = False
    origin = str(payload.get("origin", "")) if payload.get("origin") is not None else None
    for key in _ASSET_FIELD_ORDER:
        value = payload.get(key)
        if value is None:
            continue
        rows_added = True
        table.add_row(
            Text(_field_label(key, origin=origin)),
            Text(_field_value(value)),
        )

    if not rows_added:
        table.add_row(Text("", style="dim"), Text("(empty)", style="dim"))
    return table


def _asset_origin(value_struct: object) -> str:
    source = getattr(value_struct, "source", None)
    location = getattr(value_struct, "location", None)
    location_type = getattr(location, "type", None)
    if source is not None:
        return "copied"
    if location_type == "remote":
        return "remote"
    return "local"


def _representation_name(representation: object) -> str | None:
    if representation is None:
        return None
    name = getattr(representation, "name", None)
    if name is not None:
        return str(name)
    representation_type = getattr(representation, "type", None)
    if representation_type is not None:
        return str(representation_type)
    if isinstance(representation, str):
        return representation
    return None


def _freshness_text(freshness: object) -> str | None:
    if freshness is None:
        return None

    name = getattr(freshness, "name", None) or getattr(freshness, "type", None)
    if name is None:
        return None

    attributes = getattr(freshness, "attributes", None)
    duration = attributes.get("duration") if isinstance(attributes, dict) else None
    if name == "ttl" and duration is not None:
        return f"ttl({duration})"
    return str(name)


def _field_label(key: str, *, origin: str | None) -> str:
    if key == "resolvedUrl":
        return "resolved url"
    if key == "contentHash":
        return "content hash"
    if key == "digestType":
        return "digest type"
    if key == "lastModified":
        return "last modified"
    if key == "updateTime":
        return "update time"
    if key == "fetchedAt":
        return "fetched at"
    if key == "path":
        return "cache path" if origin == "remote" else "path"
    return key


def _field_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
