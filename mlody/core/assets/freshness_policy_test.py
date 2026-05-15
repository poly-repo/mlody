"""Tests for asset freshness policy helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mlody.common.struct import Struct
from mlody.core.assets.freshness_policy import (
    freshness_policy_from_struct,
    remote_metadata_indicates_change,
    should_revalidate_http_asset,
    should_refresh_copied_asset,
)
from mlody.core.assets.manifest import (
    HttpAssetManifest,
    HttpAssetManifestLocal,
    HttpAssetManifestRemote,
    HttpAssetManifestRequest,
)


def _manual() -> Struct:
    return Struct(kind="freshness", type="manual", name="manual", attributes={})


def _always() -> Struct:
    return Struct(kind="freshness", type="always", name="always", attributes={})


def _ttl(duration: str) -> Struct:
    return Struct(
        kind="freshness",
        type="ttl",
        name="ttl",
        attributes={"duration": duration},
    )


def _manifest(*, checked_at: str, downloaded_at: str) -> HttpAssetManifest:
    return HttpAssetManifest(
        request=HttpAssetManifestRequest(
            uri="https://example.com/data.csv",
            cache_key="sha256:test",
            resolved_url="https://example.com/data.csv",
        ),
        remote=HttpAssetManifestRemote(
            digest="etag-1",
            digest_type="etag",
            length=10,
            update_time="2026-05-14T12:00:00Z",
            metadata_checked_at=checked_at,
        ),
        local=HttpAssetManifestLocal(
            payload_relpath="payload.csv",
            content_hash="abc123",
            size_bytes=10,
            downloaded_at=downloaded_at,
        ),
    )


def test_freshness_policy_from_struct_parses_ttl_duration() -> None:
    policy = freshness_policy_from_struct(_ttl("P1D"))

    assert policy.kind == "ttl"
    assert policy.max_age == timedelta(days=1)


def test_should_revalidate_http_asset_respects_manual_and_always() -> None:
    manifest = _manifest(
        checked_at="2026-05-14T12:00:00Z",
        downloaded_at="2026-05-14T12:00:00Z",
    )
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    assert should_revalidate_http_asset(_manual(), manifest, now=now) is False
    assert should_revalidate_http_asset(_always(), manifest, now=now) is True


def test_should_revalidate_http_asset_uses_metadata_check_age_for_ttl() -> None:
    recent = _manifest(
        checked_at="2026-05-14T23:00:00Z",
        downloaded_at="2026-05-14T12:00:00Z",
    )
    stale = _manifest(
        checked_at="2026-05-13T23:00:00Z",
        downloaded_at="2026-05-13T23:00:00Z",
    )
    now = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)

    assert should_revalidate_http_asset(_ttl("P1D"), recent, now=now) is False
    assert should_revalidate_http_asset(_ttl("P1D"), stale, now=now) is True


def test_should_refresh_copied_asset_uses_destination_mtime() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=2)).timestamp()
    stale = (now - timedelta(days=2)).timestamp()

    assert should_refresh_copied_asset(_ttl("P1D"), destination_mtime=recent, now=now) is False
    assert should_refresh_copied_asset(_ttl("P1D"), destination_mtime=stale, now=now) is True


def test_remote_metadata_indicates_change_uses_comparable_fields() -> None:
    manifest = _manifest(
        checked_at="2026-05-14T12:00:00Z",
        downloaded_at="2026-05-14T12:00:00Z",
    )

    assert remote_metadata_indicates_change(
        manifest,
        {"digest": "etag-1", "length": 10, "update_time": "2026-05-14T12:00:00Z"},
    ) is False
    assert remote_metadata_indicates_change(
        manifest,
        {"digest": "etag-2", "length": 10, "update_time": "2026-05-14T12:00:00Z"},
    ) is True
