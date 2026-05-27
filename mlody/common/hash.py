"""Helpers for semantic mlody content hashes."""

from __future__ import annotations

from mlody.core.assets.resolution import asset_from_value


def hash(value: object, *, db_conn: object | None = None) -> str | None:
    """Return the semantic content hash for *value* when one is available.

    Today only remote runtime values participate. Materialization is delegated
    to the asset layer so freshness checks, revalidation, and downloads happen
    in exactly one place.
    """
    if getattr(value, "kind", None) != "value":
        return None

    location = getattr(value, "location", None)
    if getattr(location, "type", None) != "remote":
        return None

    asset = asset_from_value(value, db_conn=db_conn)
    if asset is None:
        return None
    return asset.materialize().content_hash


__all__ = ["hash"]
