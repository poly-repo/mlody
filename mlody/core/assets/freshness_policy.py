"""Freshness helpers for asset caching and revalidation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from mlody.core.assets.manifest import HttpAssetManifest

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_HUMAN_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+)\s*(?P<unit>day|days|d|hour|hours|h|minute|minutes|min|m|second|seconds|sec|s)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Normalized freshness policy used by the asset layer."""

    kind: str
    max_age: timedelta | None = None


def freshness_policy_from_struct(freshness: object | None) -> FreshnessPolicy:
    """Normalize a runtime freshness struct into a small policy object."""
    if freshness is None:
        return FreshnessPolicy(kind="unspecified")

    kind = getattr(freshness, "type", None) or getattr(freshness, "name", None)
    kind_text = str(kind) if kind is not None else "unspecified"
    if kind_text == "ttl":
        attrs = getattr(freshness, "attributes", None)
        duration_value = attrs.get("duration") if isinstance(attrs, dict) else None
        duration = _parse_duration(duration_value)
        if duration is None:
            return FreshnessPolicy(kind="ttl", max_age=timedelta(0))
        return FreshnessPolicy(kind="ttl", max_age=duration)
    if kind_text in {"always", "manual", "scheduled"}:
        return FreshnessPolicy(kind=kind_text)
    return FreshnessPolicy(kind=kind_text)


def should_revalidate_http_asset(
    freshness: object | None,
    manifest: HttpAssetManifest | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a cached HTTP asset should re-check the remote."""
    if manifest is None:
        return True
    if not manifest.local.payload_relpath or not manifest.local.content_hash:
        return True

    policy = freshness_policy_from_struct(freshness)
    if policy.kind == "always":
        return True
    if policy.kind != "ttl":
        return False
    if policy.max_age is None or policy.max_age <= timedelta(0):
        return True

    anchor = _parse_timestamp(
        manifest.remote.metadata_checked_at or manifest.local.downloaded_at
    )
    if anchor is None:
        return True
    return _current_time(now) - anchor >= policy.max_age


def should_refresh_copied_asset(
    freshness: object | None,
    *,
    destination_mtime: float,
    now: datetime | None = None,
) -> bool:
    """Return whether a cached copied artifact should consult its upstream."""
    policy = freshness_policy_from_struct(freshness)
    if policy.kind == "always":
        return True
    if policy.kind != "ttl":
        return False
    if policy.max_age is None or policy.max_age <= timedelta(0):
        return True

    copied_at = datetime.fromtimestamp(destination_mtime, tz=timezone.utc)
    return _current_time(now) - copied_at >= policy.max_age


def remote_metadata_indicates_change(
    manifest: HttpAssetManifest,
    metadata: dict[str, object],
) -> bool:
    """Return whether newly fetched remote metadata implies new content."""
    comparisons: list[bool] = []

    digest = _text(metadata.get("digest"))
    if digest is not None and manifest.remote.digest is not None:
        comparisons.append(digest != manifest.remote.digest)

    update_time = _text(metadata.get("update_time"))
    if update_time is not None and manifest.remote.update_time is not None:
        comparisons.append(update_time != manifest.remote.update_time)

    length = _int(metadata.get("length"))
    if length is not None and manifest.remote.length is not None:
        comparisons.append(length != manifest.remote.length)

    if not comparisons:
        return True
    return any(comparisons)


def manifest_with_refreshed_remote_metadata(
    manifest: HttpAssetManifest,
    metadata: dict[str, object],
    *,
    checked_at: str,
) -> HttpAssetManifest:
    """Return ``manifest`` with refreshed remote metadata and check time."""
    digest = _text(metadata.get("digest"))
    digest_type = _text(metadata.get("digest_type"))
    length = _int(metadata.get("length"))
    update_time = _text(metadata.get("update_time"))
    etag = _text(metadata.get("etag"))
    last_modified = _text(metadata.get("last_modified"))
    remote = replace(
        manifest.remote,
        digest=digest if digest is not None else manifest.remote.digest,
        digest_type=(
            digest_type if digest_type is not None else manifest.remote.digest_type
        ),
        length=length if length is not None else manifest.remote.length,
        update_time=(
            update_time if update_time is not None else manifest.remote.update_time
        ),
        etag=etag if etag is not None else manifest.remote.etag,
        last_modified=(
            last_modified
            if last_modified is not None
            else manifest.remote.last_modified
        ),
        metadata_checked_at=checked_at,
    )
    request = replace(
        manifest.request,
        resolved_url=_text(metadata.get("url")) or manifest.request.resolved_url,
    )
    return replace(manifest, request=request, remote=remote)


def _parse_duration(value: object) -> timedelta | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    iso_match = _ISO_DURATION_RE.fullmatch(text)
    if iso_match is not None:
        days = int(iso_match.group("days") or 0)
        hours = int(iso_match.group("hours") or 0)
        minutes = int(iso_match.group("minutes") or 0)
        seconds = int(iso_match.group("seconds") or 0)
        total = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return total if total > timedelta(0) else None

    human_match = _HUMAN_DURATION_RE.fullmatch(text)
    if human_match is None:
        return None

    amount = int(human_match.group("value"))
    unit = human_match.group("unit").lower()
    if unit in {"day", "days", "d"}:
        return timedelta(days=amount)
    if unit in {"hour", "hours", "h"}:
        return timedelta(hours=amount)
    if unit in {"minute", "minutes", "min", "m"}:
        return timedelta(minutes=amount)
    if unit in {"second", "seconds", "sec", "s"}:
        return timedelta(seconds=amount)
    return None


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _current_time(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
