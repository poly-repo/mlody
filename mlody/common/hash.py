"""Helpers for semantic mlody content hashes."""

from __future__ import annotations

from mlody.core.assets.resolution import asset_from_value


def hash(value: object, *, db_conn: object | None = None) -> str | None:
    """Return the semantic content hash for *value* when one is available.

    Remote-like values materialize through the asset layer so freshness checks,
    revalidation, and staged copies happen in exactly one place. Source-backed
    local values follow their resolved ``_source_value`` chain until a remote
    or SSH-backed source is reached, allowing ``hash(resolve(...))`` to work on
    cached artifacts and similar wrappers.
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

    location = getattr(value, "location", None)
    if getattr(location, "type", None) in {"remote", "ssh"}:
        asset = asset_from_value(value, db_conn=db_conn)
        if asset is None:
            return None
        return asset.materialize().content_hash

    source_value = getattr(value, "_source_value", None)
    if source_value is not None:
        return _hash_value(source_value, db_conn=db_conn, seen=seen)

    source_attr = getattr(value, "source", None)
    if getattr(source_attr, "kind", None) == "value":
        return _hash_value(source_attr, db_conn=db_conn, seen=seen)

    return None


__all__ = ["hash"]
