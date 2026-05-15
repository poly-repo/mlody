"""Shared metadata types for materialized assets."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    """Normalized metadata about an artifact and its remote origin."""

    uri: str | None
    resolved_url: str | None
    digest: str | None
    digest_type: str | None
    length: int | None
    update_time: str | None
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str | None = None
    cache_key: str | None = None
    transport: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
