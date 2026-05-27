"""File-system cache statistics for the Stage HTTP server."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import TypedDict


class CacheAssetEntry(TypedDict):
    hash: str
    path: str
    uri: str | None
    value_name: str | None
    columns: str | None
    size_bytes: int
    kind: str
    downloaded_at: str | None
    referenced: bool


def _read_parquet_columns(path: Path) -> str | None:
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415
        names = list(pq.read_schema(str(path)).names)
        if not names:
            return None
        preview = names[:4]
        suffix = f" +{len(names) - 4} more" if len(names) > 4 else ""
        return ", ".join(preview) + suffix
    except Exception:  # noqa: BLE001
        return None


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _iter_value_rows(workspace: object):  # type: ignore[return]
    registry_view = getattr(workspace, "registry_view", None)
    iter_registry_items = getattr(registry_view, "iter_registry_items", None)
    if callable(iter_registry_items):
        raw_items = iter_registry_items()
    else:
        registry = getattr(getattr(workspace, "evaluator", None), "registry", None)
        all_items = getattr(registry, "all", None)
        raw_items = all_items.items() if hasattr(all_items, "items") else ()

    for raw_key, value in raw_items:
        if not isinstance(raw_key, tuple) or len(raw_key) != 3:
            continue
        raw_kind, raw_stem, raw_name = raw_key
        if raw_kind == "value":
            label = f"{raw_stem}:{raw_name}" if raw_stem else raw_name
            yield label, value


def _build_referenced_hashes(workspace: object) -> set[str]:
    from mlody.core.location_specs import RemoteLocationSpec  # noqa: PLC0415

    hashes: set[str] = set()
    for _name, value in _iter_value_rows(workspace):
        location = getattr(value, "location", None)
        spec = RemoteLocationSpec.from_location(location)
        if spec is not None:
            hashes.add(hashlib.sha256(spec.uri.encode("utf-8")).hexdigest())
    return hashes


def _build_derived_value_index(workspace: object) -> dict[str, str]:
    """Return {parquet_stem: value_name} for all current workspace derived values."""
    from mlody.core.location_specs import derived_location_spec_from_value  # noqa: PLC0415

    index: dict[str, str] = {}
    for name, value in _iter_value_rows(workspace):
        try:
            spec = derived_location_spec_from_value(value)
        except Exception:  # noqa: BLE001
            continue
        if spec is not None:
            index[spec.output_path.stem] = name
    return index


def _load_db_index(
    conn: sqlite3.Connection | None,
) -> dict[str, dict[str, str | None]]:
    """Return {hex_hash: {value_name, uri}} from external_assets."""
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT cache_key, value_name, uri FROM external_assets"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, dict[str, str | None]] = {}
    for cache_key, value_name, uri in rows:
        if isinstance(cache_key, str) and cache_key.startswith("sha256:"):
            hex_hash = cache_key[len("sha256:"):]
            result[hex_hash] = {
                "value_name": value_name if isinstance(value_name, str) else None,
                "uri": uri if isinstance(uri, str) else None,
            }
    return result


def _scan_http_assets(
    http_root: Path,
    referenced_hashes: set[str],
    db_index: dict[str, dict[str, str | None]],
) -> list[CacheAssetEntry]:
    if not http_root.is_dir():
        return []
    entries: list[CacheAssetEntry] = []
    for d in http_root.iterdir():
        if not d.is_dir():
            continue
        hex_hash = d.name
        size_bytes = _dir_size(d)
        uri: str | None = None
        downloaded_at: str | None = None
        manifest_path = d / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                uri = manifest.get("request", {}).get("uri") or None
                downloaded_at = manifest.get("local", {}).get("downloaded_at") or None
            except Exception:  # noqa: BLE001
                pass
        db_row = db_index.get(hex_hash, {})
        if uri is None:
            uri = db_row.get("uri")
        value_name: str | None = db_row.get("value_name")
        entries.append(
            CacheAssetEntry(
                hash=hex_hash,
                path=str(d),
                uri=uri,
                value_name=value_name,
                columns=None,
                size_bytes=size_bytes,
                kind="http",
                downloaded_at=downloaded_at,
                referenced=hex_hash in referenced_hashes,
            )
        )
    return entries


def _scan_derived_parquets(
    derived_root: Path,
    derived_value_index: dict[str, str],
) -> list[CacheAssetEntry]:
    if not derived_root.is_dir():
        return []
    entries: list[CacheAssetEntry] = []
    for f in derived_root.iterdir():
        if not f.is_file() or f.suffix != ".parquet":
            continue
        value_name = derived_value_index.get(f.stem)
        columns = None if value_name is not None else _read_parquet_columns(f)
        entries.append(
            CacheAssetEntry(
                hash=f.stem,
                path=str(f),
                uri=None,
                value_name=value_name,
                columns=columns,
                size_bytes=f.stat().st_size,
                kind="derived",
                downloaded_at=None,
                referenced=value_name is not None,
            )
        )
    return entries


def gather_cache_status(
    workspace: object,
    db_conn: sqlite3.Connection | None,
    cache_root: Path,
) -> dict[str, object]:
    """Scan the mlody file-system cache and return a serialisable status dict."""
    referenced_hashes = _build_referenced_hashes(workspace) if workspace is not None else set()
    derived_value_index = _build_derived_value_index(workspace) if workspace is not None else {}
    db_index = _load_db_index(db_conn)

    http_entries = _scan_http_assets(
        cache_root / "assets" / "http",
        referenced_hashes,
        db_index,
    )
    derived_entries = _scan_derived_parquets(cache_root / "derived", derived_value_index)

    all_entries: list[CacheAssetEntry] = http_entries + derived_entries
    total_size_bytes = sum(e["size_bytes"] for e in all_entries)
    unreferenced = [e for e in all_entries if not e["referenced"]]
    top_assets = sorted(all_entries, key=lambda e: e["size_bytes"], reverse=True)[:10]
    unreferenced_sorted = sorted(unreferenced, key=lambda e: e["size_bytes"], reverse=True)

    return {
        "cache_root": str(cache_root),
        "total_size_bytes": total_size_bytes,
        "http_count": len(http_entries),
        "derived_count": len(derived_entries),
        "unreferenced_count": len(unreferenced),
        "unreferenced": unreferenced_sorted,
        "top_assets": top_assets,
    }


def clean_cache(
    workspace: object,
    db_conn: sqlite3.Connection | None,
    cache_root: Path,
    *,
    clean_all: bool = False,
) -> dict[str, object]:
    """Delete cached entries and return a summary of what was freed."""
    referenced_hashes = _build_referenced_hashes(workspace) if workspace is not None else set()
    derived_value_index = _build_derived_value_index(workspace) if workspace is not None else {}
    db_index = _load_db_index(db_conn)

    http_entries = _scan_http_assets(cache_root / "assets" / "http", referenced_hashes, db_index)
    derived_entries = _scan_derived_parquets(cache_root / "derived", derived_value_index)

    all_entries: list[CacheAssetEntry] = http_entries + derived_entries
    to_delete = all_entries if clean_all else [e for e in all_entries if not e["referenced"]]

    deleted: list[CacheAssetEntry] = []
    freed_bytes = 0
    for entry in to_delete:
        p = Path(entry["path"])
        if not p.exists():
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            freed_bytes += entry["size_bytes"]
            deleted.append(entry)
        except Exception:  # noqa: BLE001
            pass

    return {
        "deleted_count": len(deleted),
        "freed_bytes": freed_bytes,
        "deleted": deleted,
    }
