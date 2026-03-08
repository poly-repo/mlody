"""Cache operations for materialised workspace directories."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mlody.resolver.errors import CorruptCacheError, LockBusyError

# The sentinel file whose presence marks a cache entry as complete
_SENTINEL_RELATIVE = Path("mlody") / "roots.mlody"


def cache_dir(cache_root: Path, sha: str) -> Path:
    """Return the expected cache directory path for a given full SHA."""
    return cache_root / sha


def check_cache(
    cache_root: Path, sha: str
) -> Literal["hit", "miss", "corrupt"]:
    """Determine whether a workspace is cached, missing, or corrupt.

    A 'hit' means the sentinel roots.mlody exists inside the cache dir.
    A 'corrupt' means the directory exists but the sentinel is absent —
    this indicates a prior materialisation that failed after creating the dir.
    A 'miss' means the directory does not exist yet.
    """
    d = cache_dir(cache_root, sha)
    if not d.exists():
        return "miss"
    sentinel = d / _SENTINEL_RELATIVE
    if sentinel.exists():
        return "hit"
    return "corrupt"


def acquire_lock(cache_root: Path, sha: str) -> Path:
    """Atomically create a lock file, raising LockBusyError on contention.

    Uses O_CREAT | O_EXCL semantics (open mode "x") so only one process can
    acquire the lock for a given SHA at a time. Callers must always release the
    lock in a finally block via release_lock().
    """
    lock_path = cache_root / f"{sha}.lock"
    try:
        open(lock_path, "x").close()  # noqa: WPS515 — exclusive creation is the intent
    except FileExistsError:
        raise LockBusyError(lock_path)
    return lock_path


def release_lock(lock_path: Path) -> None:
    """Remove the lock file, tolerating missing_ok for robustness.

    Called in finally blocks; passing missing_ok=True means a double-release
    (e.g., from concurrent error handling) is harmless.
    """
    lock_path.unlink(missing_ok=True)


def write_metadata(
    cache_root: Path,
    sha: str,
    requested_ref: str,
    repo_url: str,
) -> None:
    """Write provenance JSON alongside the cache directory.

    The file is written only if it does not already exist — a prior
    materialisation may have written it, and we don't want to overwrite it
    with potentially different metadata (e.g., a different requested_ref alias
    for the same SHA).
    """
    meta_path = cache_root / f"{sha}-meta.json"
    if meta_path.exists():
        return

    payload = {
        "requested_ref": requested_ref,
        "resolved_sha": sha,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo_url,
    }
    meta_path.write_text(json.dumps(payload, indent=2))


def ensure_cache_root(cache_root: Path) -> None:
    """Create the cache root directory with user-only permissions.

    mode=0o700 ensures that cached workspace clones (which may contain
    sensitive config) are not readable by other users on the same machine.
    """
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
