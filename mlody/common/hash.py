"""Helpers for semantic mlody content hashes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mlody.common.struct import Struct, struct_like_to_struct
from mlody.core.assets.resolution import asset_from_value


def hash(value: object, *, db_conn: object | None = None) -> str | None:
    """Return the semantic content hash for *value* when one is available.

    Remote-like values materialize through the asset layer so freshness checks,
    revalidation, and staged copies happen in exactly one place. Source-backed
    local values hash their copied local artifact and only consult their
    upstream remote when the declared freshness policy requires a refresh.
    """
    return _hash_value(value, db_conn=db_conn, seen=set())


def _hash_value(
    value: object,
    *,
    db_conn: object | None,
    seen: set[int],
) -> str | None:
    if getattr(value, "kind", None) != "value":
        return None

    value_id = id(value)
    if value_id in seen:
        return None
    seen.add(value_id)

    resolved_value = getattr(value, "_resolved_value", None)
    if resolved_value is not None and resolved_value is not value:
        return _hash_value(resolved_value, db_conn=db_conn, seen=seen)

    location = getattr(value, "location", None)
    if getattr(location, "type", None) in {"remote", "https", "ssh"}:
        return _materialized_content_hash(value, db_conn=db_conn)

    source_value = getattr(value, "_source_value", None)
    source_attr = getattr(value, "source", None)
    if source_value is not None or getattr(source_attr, "kind", None) == "value":
        content_hash = _materialized_content_hash(value, db_conn=db_conn)
        if content_hash is not None:
            return content_hash

    if source_value is not None:
        return _hash_value(source_value, db_conn=db_conn, seen=seen)

    if getattr(source_attr, "kind", None) == "value":
        return _hash_value(source_attr, db_conn=db_conn, seen=seen)

    inline_data = getattr(location, "data", None)
    if inline_data is not None:
        return _structured_payload_hash(inline_data)

    return None


def _materialized_content_hash(
    value: object,
    *,
    db_conn: object | None,
) -> str | None:
    asset = asset_from_value(value, db_conn=db_conn)
    if asset is None:
        return None

    materialized = asset.materialize()
    if materialized.content_hash is not None:
        return materialized.content_hash
    return _file_content_hash(materialized.path)


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
    if isinstance(value, Struct):
        return {
            str(name): _json_payload(child)
            for name, child in value.as_mapping().items()
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


__all__ = ["hash"]
