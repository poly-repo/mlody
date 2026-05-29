"""Helpers for semantic mlody hashes for values and tasks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from mlody.common.struct import (
    Struct,
    is_struct_like,
    struct_like_as_mapping,
    struct_like_to_struct,
)
from mlody.core.assets.resolution import asset_from_value

_TASK_HASH_VERSION = 1
_VALUE_FINGERPRINT_VERSION = 1
_JSON_SKIP = object()
_NORMALIZED_STRUCT_SKIP_KEYS = frozenset(
    {
        "description",
        "methods",
        "raw",
        "lineage",
        "_allowed_attrs",
        "_attrs_mandatory",
        "_canonical_for_attrs",
        "_context_attr_policies",
        "_entity_type",
        "_lineage",
        "_producer_task",
        "_resolved_label",
        "_resolved_value",
        "_root_kind",
        "_source_range",
        "_source_value",
        "validator",
        "canonical",
    }
)


def combine_commit_and_patch_sha(commit_sha: str, local_patch_sha: str | None) -> str:
    """Return the stable base hash for one repo commit plus local changes."""
    if not local_patch_sha:
        return commit_sha
    return hashlib.sha256(f"{commit_sha}:{local_patch_sha}".encode("utf-8")).hexdigest()


def hash(value: object, *, db_conn: object | None = None) -> str | None:
    """Return the semantic hash for *value* when one is available.

    Remote-like values materialize through the asset layer so freshness checks,
    revalidation, and staged copies happen in exactly one place. Source-backed
    local values hash their copied local artifact and only consult their
    upstream remote when the declared freshness policy requires a refresh.
    Tasks hash deterministically from their repo base hash plus the effective
    hashes of their config and input values.
    """
    resolved_value = getattr(value, "_resolved_value", None)
    if resolved_value is not None and resolved_value is not value:
        return hash(resolved_value, db_conn=db_conn)

    kind = getattr(value, "kind", None)
    if kind == "task":
        return _hash_task(value, db_conn=db_conn, seen=set())
    if kind == "value":
        return _hash_value(value, db_conn=db_conn, seen=set())
    return None


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


def _hash_task(
    task: object,
    *,
    db_conn: object | None,
    seen: set[int],
) -> str:
    task_id = id(task)
    if task_id in seen:
        task_name = getattr(task, "name", "<unknown>")
        raise ValueError(f"cycle detected while hashing task {task_name!r}")

    seen.add(task_id)
    try:
        payload = {
            "version": _TASK_HASH_VERSION,
            "task_base_hash": _task_base_hash(task),
            "task_identity": _task_identity_payload(task),
            "config": _task_port_hashes(
                getattr(task, "config", None),
                db_conn=db_conn,
                seen=seen,
            ),
            "inputs": _task_port_hashes(
                getattr(task, "inputs", None),
                db_conn=db_conn,
                seen=seen,
            ),
        }
        return _structured_payload_hash(payload)
    finally:
        seen.remove(task_id)


def _task_base_hash(task: object) -> str | None:
    repo_root = _repo_root_for_task(task)
    if repo_root is None:
        return None

    commit_sha = _git_stdout(repo_root, "rev-parse", "HEAD")
    if not commit_sha:
        return None

    from mlody.common.git_diff import local_patch_sha  # noqa: PLC0415

    return combine_commit_and_patch_sha(commit_sha, local_patch_sha(repo_root, commit_sha))


def _repo_root_for_task(task: object) -> Path | None:
    candidates: list[Path] = []

    source_range = getattr(task, "_source_range", None)
    filepath = getattr(source_range, "filepath", None)
    if isinstance(filepath, str) and filepath:
        candidates.append(Path(filepath).expanduser().resolve().parent)

    workspace_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace_dir:
        candidates.append(Path(workspace_dir).expanduser().resolve())

    candidates.append(Path.cwd().resolve())

    seen_candidates: set[Path] = set()
    for candidate in candidates:
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        repo_root = _git_toplevel(candidate)
        if repo_root is not None:
            return repo_root
    return None


def _git_toplevel(candidate: Path) -> Path | None:
    root = _git_stdout(candidate, "rev-parse", "--show-toplevel")
    if not root:
        return None
    return Path(root)


def _git_stdout(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _task_identity_payload(task: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": str(getattr(task, "name", "")),
    }
    source_range = _normalize_payload(getattr(task, "_source_range", None))
    if source_range is not _JSON_SKIP and source_range is not None:
        payload["source_range"] = source_range
    return payload


def _task_port_hashes(
    container: object,
    *,
    db_conn: object | None,
    seen: set[int],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in sorted(_named_value_items(container), key=lambda item: item[0]):
        result[name] = _value_fingerprint_hash(
            value,
            db_conn=db_conn,
            seen_tasks=seen,
            seen_values=set(),
        )
    return result


def _named_value_items(container: object) -> list[tuple[str, object]]:
    if container is None:
        return []
    if isinstance(container, dict):
        return [(str(name), value) for name, value in container.items()]
    if is_struct_like(container):
        return [
            (str(name), value)
            for name, value in struct_like_as_mapping(container).items()
        ]
    if isinstance(container, (list, tuple)):
        items: list[tuple[str, object]] = []
        for index, value in enumerate(container):
            name = getattr(value, "name", None)
            item_name = str(name) if isinstance(name, str) and name else str(index)
            items.append((item_name, value))
        return items
    return []


def _value_fingerprint_hash(
    value: object,
    *,
    db_conn: object | None,
    seen_tasks: set[int],
    seen_values: set[int],
) -> str:
    resolved_value = getattr(value, "_resolved_value", None)
    if resolved_value is not None and resolved_value is not value:
        return _value_fingerprint_hash(
            resolved_value,
            db_conn=db_conn,
            seen_tasks=seen_tasks,
            seen_values=seen_values,
        )

    value_id = id(value)
    if value_id in seen_values:
        value_name = getattr(value, "name", "<unknown>")
        raise ValueError(f"cycle detected while hashing value {value_name!r}")

    seen_values.add(value_id)
    try:
        upstream_payload = _upstream_hash_payload(
            value,
            db_conn=db_conn,
            seen_tasks=seen_tasks,
            seen_values=seen_values,
        )

        payload: dict[str, object] = {
            "version": _VALUE_FINGERPRINT_VERSION,
            "descriptor": _value_descriptor_payload(value),
        }

        semantic_hash = _semantic_hash_for_fingerprint(
            value,
            db_conn=db_conn,
            upstream_payload=upstream_payload,
        )
        if semantic_hash is not None:
            payload["semantic_hash"] = semantic_hash

        if upstream_payload is not None:
            payload["upstream"] = upstream_payload

        return _structured_payload_hash(payload)
    finally:
        seen_values.remove(value_id)


def _semantic_hash_for_fingerprint(
    value: object,
    *,
    db_conn: object | None,
    upstream_payload: dict[str, str] | None,
) -> str | None:
    if upstream_payload is not None and "producer_task_hash" in upstream_payload:
        return None
    return _hash_value(value, db_conn=db_conn, seen=set())


def _value_descriptor_payload(value: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, raw_value in (
        ("type", getattr(value, "type", None)),
        ("location", getattr(value, "location", None)),
        ("freshness", getattr(value, "freshness", None)),
        ("representation", getattr(value, "representation", None)),
        ("default", getattr(value, "default", None)),
        ("unit", getattr(value, "unit", None)),
        ("constraint", getattr(value, "constraint", None)),
    ):
        normalized = _normalize_payload(raw_value)
        if normalized is _JSON_SKIP or normalized is None:
            continue
        payload[key] = normalized

    normalized_source = _normalize_source_ref(getattr(value, "source", None))
    if normalized_source is not _JSON_SKIP and normalized_source is not None:
        payload["source"] = normalized_source

    return payload


def _normalize_source_ref(source: object) -> object:
    if source is None:
        return None
    if isinstance(source, str):
        return source
    if _is_port_ref(source):
        return {
            "kind": "port_ref",
            "task": str(getattr(source, "task")),
            "port": str(getattr(source, "port")),
        }
    if getattr(source, "kind", None) == "value":
        name = getattr(source, "name", None)
        if isinstance(name, str) and name:
            return {
                "kind": "value",
                "name": name,
            }
    return _normalize_payload(source)


def _upstream_hash_payload(
    value: object,
    *,
    db_conn: object | None,
    seen_tasks: set[int],
    seen_values: set[int],
) -> dict[str, str] | None:
    upstream_value = getattr(value, "_source_value", None)
    if upstream_value is None:
        source_attr = getattr(value, "source", None)
        if getattr(source_attr, "kind", None) == "value":
            upstream_value = source_attr

    if upstream_value is None:
        return None

    producer_task = getattr(upstream_value, "_producer_task", None)
    if producer_task is not None:
        return {
            "producer_task_hash": _hash_task(
                producer_task,
                db_conn=db_conn,
                seen=seen_tasks,
            )
        }

    return {
        "value_hash": _value_fingerprint_hash(
            upstream_value,
            db_conn=db_conn,
            seen_tasks=seen_tasks,
            seen_values=seen_values,
        )
    }


def _normalize_payload(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if callable(value) and not isinstance(value, type):
        return _JSON_SKIP
    if _is_port_ref(value):
        return {
            "task": str(getattr(value, "task")),
            "port": str(getattr(value, "port")),
        }
    if is_struct_like(value):
        payload: dict[str, object] = {}
        for key, child in sorted(struct_like_as_mapping(value).items()):
            if key.startswith("_") or key in _NORMALIZED_STRUCT_SKIP_KEYS:
                continue
            normalized = _normalize_payload(child)
            if normalized is _JSON_SKIP:
                continue
            payload[str(key)] = normalized
        return payload
    if isinstance(value, dict):
        payload: dict[str, object] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            normalized = _normalize_payload(child)
            if normalized is _JSON_SKIP:
                continue
            payload[str(key)] = normalized
        return payload
    if isinstance(value, (list, tuple)):
        return [
            normalized
            for child in value
            if (normalized := _normalize_payload(child)) is not _JSON_SKIP
        ]
    if isinstance(value, (set, frozenset)):
        normalized_items = [
            normalized
            for child in value
            if (normalized := _normalize_payload(child)) is not _JSON_SKIP
        ]
        return sorted(normalized_items, key=_json_sort_key)
    return value


def _json_sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _is_port_ref(value: object) -> bool:
    return hasattr(value, "task") and hasattr(value, "port")


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


__all__ = ["combine_commit_and_patch_sha", "hash"]
