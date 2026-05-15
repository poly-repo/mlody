"""Manifest schema helpers for persistent remote asset caches."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class HttpAssetManifestRequest:
    uri: str
    cache_key: str
    resolved_url: str | None = None


@dataclass(frozen=True, slots=True)
class HttpAssetManifestRemote:
    digest: str | None = None
    digest_type: str | None = None
    length: int | None = None
    update_time: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    metadata_checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class HttpAssetManifestLocal:
    payload_relpath: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    downloaded_at: str | None = None


@dataclass(frozen=True, slots=True)
class HttpAssetManifest:
    request: HttpAssetManifestRequest
    remote: HttpAssetManifestRemote
    local: HttpAssetManifestLocal
    transport: str = "http"
    schema_version: int = MANIFEST_SCHEMA_VERSION
    extra: dict[str, object] = field(default_factory=dict)


def cache_key_for_uri(uri: str) -> str:
    """Return the stable cache identity for a request URI."""
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_manifest(path: Path) -> HttpAssetManifest:
    """Load and validate an HTTP asset manifest."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest at {path} must contain a JSON object")

    schema_version = payload.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported manifest schema version {schema_version!r} at {path}"
        )

    request_payload = payload.get("request")
    remote_payload = payload.get("remote")
    local_payload = payload.get("local")
    extra_payload = payload.get("extra", {})
    if not isinstance(request_payload, dict):
        raise ValueError(f"Manifest at {path} is missing request metadata")
    if not isinstance(remote_payload, dict):
        raise ValueError(f"Manifest at {path} is missing remote metadata")
    if not isinstance(local_payload, dict):
        raise ValueError(f"Manifest at {path} is missing local metadata")
    if not isinstance(extra_payload, dict):
        raise ValueError(f"Manifest at {path} has non-object extra metadata")

    known_root_keys = {"schema_version", "transport", "request", "remote", "local", "extra"}
    merged_extra = dict(extra_payload)
    for key, value in payload.items():
        if key not in known_root_keys:
            merged_extra[key] = value

    return HttpAssetManifest(
        schema_version=int(schema_version),
        transport=str(payload.get("transport") or "http"),
        request=HttpAssetManifestRequest(
            uri=str(request_payload["uri"]),
            cache_key=str(request_payload["cache_key"]),
            resolved_url=_optional_text(request_payload.get("resolved_url")),
        ),
        remote=HttpAssetManifestRemote(
            digest=_optional_text(remote_payload.get("digest")),
            digest_type=_optional_text(remote_payload.get("digest_type")),
            length=_optional_int(remote_payload.get("length")),
            update_time=_optional_text(remote_payload.get("update_time")),
            etag=_optional_text(remote_payload.get("etag")),
            last_modified=_optional_text(remote_payload.get("last_modified")),
            metadata_checked_at=_optional_text(remote_payload.get("metadata_checked_at")),
        ),
        local=HttpAssetManifestLocal(
            payload_relpath=_optional_text(local_payload.get("payload_relpath")),
            content_hash=_optional_text(local_payload.get("content_hash")),
            size_bytes=_optional_int(local_payload.get("size_bytes")),
            downloaded_at=_optional_text(local_payload.get("downloaded_at")),
        ),
        extra=merged_extra,
    )


def write_manifest(path: Path, manifest: HttpAssetManifest) -> None:
    """Persist *manifest* atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": manifest.schema_version,
        "transport": manifest.transport,
        "request": asdict(manifest.request),
        "remote": asdict(manifest.remote),
        "local": asdict(manifest.local),
        "extra": dict(manifest.extra),
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
