"""Persistent cache helpers for asset sources."""

from __future__ import annotations

import os
from pathlib import Path


def default_http_cache_root() -> Path:
    """Return the default persistent cache root for HTTP assets."""
    test_tmpdir = os.environ.get("TEST_TMPDIR")
    if test_tmpdir:
        return Path(test_tmpdir) / "mlody" / "assets" / "http"
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "mlody" / "assets" / "http"
    return Path.home() / ".cache" / "mlody" / "assets" / "http"


def ensure_cache_root(cache_root: Path) -> None:
    """Create *cache_root* with user-only permissions if needed."""
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)


def cache_dir_for_key(cache_root: Path, cache_key: str) -> Path:
    """Return the cache directory used for *cache_key*."""
    directory_name = cache_key.removeprefix("sha256:")
    return cache_root / directory_name

