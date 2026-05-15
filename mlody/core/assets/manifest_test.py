from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlody.core.assets.manifest import (
    MANIFEST_SCHEMA_VERSION,
    HttpAssetManifest,
    HttpAssetManifestLocal,
    HttpAssetManifestRemote,
    HttpAssetManifestRequest,
    cache_key_for_uri,
    load_manifest,
    write_manifest,
)


def test_cache_key_for_uri_is_stable() -> None:
    first = cache_key_for_uri("https://example.com/data.csv")
    second = cache_key_for_uri("https://example.com/data.csv")

    assert first == second
    assert first.startswith("sha256:")


def test_manifest_round_trips_with_payload(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = HttpAssetManifest(
        request=HttpAssetManifestRequest(
            uri="https://example.com/data.csv",
            cache_key=cache_key_for_uri("https://example.com/data.csv"),
            resolved_url="https://cdn.example.com/data.csv",
        ),
        remote=HttpAssetManifestRemote(
            digest="etag-123",
            digest_type="etag",
            length=17,
            update_time="2026-05-11T14:32:11Z",
            etag='"etag-123"',
            last_modified="Mon, 11 May 2026 14:32:11 GMT",
            metadata_checked_at="2026-05-14T13:23:00Z",
        ),
        local=HttpAssetManifestLocal(
            payload_relpath="payload.csv",
            content_hash="abc123",
            size_bytes=17,
            downloaded_at="2026-05-14T13:23:05Z",
        ),
        extra={"note": "keep"},
    )

    write_manifest(manifest_path, manifest)
    loaded = load_manifest(manifest_path)

    assert loaded == manifest


def test_manifest_round_trips_without_payload(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = HttpAssetManifest(
        request=HttpAssetManifestRequest(
            uri="https://example.com/data.txt",
            cache_key=cache_key_for_uri("https://example.com/data.txt"),
        ),
        remote=HttpAssetManifestRemote(
            digest=None,
            digest_type=None,
            length=None,
            update_time=None,
            metadata_checked_at="2026-05-14T13:23:00Z",
        ),
        local=HttpAssetManifestLocal(),
    )

    write_manifest(manifest_path, manifest)
    loaded = load_manifest(manifest_path)

    assert loaded == manifest


def test_manifest_preserves_unknown_extra_fields(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "transport": "http",
                "request": {
                    "uri": "https://example.com/data.csv",
                    "cache_key": cache_key_for_uri("https://example.com/data.csv"),
                },
                "remote": {},
                "local": {},
                "extra": {"foo": {"bar": 1}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_manifest(manifest_path)
    assert loaded.extra == {"foo": {"bar": 1}}

    write_manifest(manifest_path, loaded)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["extra"] == {"foo": {"bar": 1}}


def test_manifest_rejects_unknown_schema_version(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "transport": "http",
                "request": {
                    "uri": "https://example.com/data.csv",
                    "cache_key": cache_key_for_uri("https://example.com/data.csv"),
                },
                "remote": {},
                "local": {},
                "extra": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported manifest schema version"):
        load_manifest(manifest_path)
