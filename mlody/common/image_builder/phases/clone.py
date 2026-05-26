"""Phase 2: repository shallow clone with cache and file locking."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from mlody.common.git_diff import local_changes as _git_local_changes
from mlody.common.image_builder.errors import CloneError
from mlody.common.image_builder.log import info

_CACHE_ROOT_DEFAULT = Path.home() / ".cache" / "mlody" / "builds"

# What to do when the local CWD has changes relative to the pinned SHA.
#   ignore — proceed without applying changes (default)
#   error  — raise CloneError if any changes are detected
#   apply  — apply tracked-file diff and copy untracked files into the clone
DirtyPolicy = Literal["ignore", "error", "apply"]


@dataclasses.dataclass(frozen=True)
class CloneResult:
    """Outcome of ensure_clone.

    path              — path to the clone directory
    applied_patch     — unified diff that was applied (empty string if none)
    applied_untracked — relative paths of untracked files that were copied
                        (empty list if none)
    """

    path: Path
    applied_patch: str
    applied_untracked: list[str]


def _cache_dir(cache_root: Path, sha: str) -> Path:
    return cache_root / sha


def _lock_path(cache_root: Path, sha: str) -> Path:
    return cache_root / f"{sha}.lock"


def _check_cache(cache_root: Path, sha: str) -> bool:
    """Return True if a complete clone for sha exists in cache_root."""
    d = _cache_dir(cache_root, sha)
    return d.is_dir() and (d / ".git" / "HEAD").exists()


def _acquire_lock(cache_root: Path, sha: str) -> Path:
    """Atomically create a lock file; raise CloneError on contention."""
    lock = _lock_path(cache_root, sha)
    try:
        lock.open("x").close()
    except FileExistsError:
        raise CloneError(
            f"Another process is already cloning SHA {sha}",
            sha=sha,
            lock=str(lock),
        )
    return lock


def _release_lock(lock: Path) -> None:
    lock.unlink(missing_ok=True)


def _run_git(args: list[str], cwd: Path | None = None) -> None:
    """Run a git command; raise CloneError on non-zero exit."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CloneError(
            f"git command failed: {' '.join(args)}",
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )


def _run_bazel(args: list[str], cwd: Path) -> None:
    """Run a bazel command; raise CloneError on non-zero exit."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CloneError(
            f"bazel command failed: {' '.join(args)}",
            cwd=str(cwd),
            stderr=result.stderr.strip(),
        )


def _clone_from(source: str, sha: str, dest: Path) -> None:
    """Clone *source* at *sha* into *dest* using a shallow fetch."""
    _run_git(["git", "clone", "--no-checkout", source, str(dest)])
    _run_git(["git", "fetch", "--depth", "1", "origin", sha], cwd=dest)
    _run_git(["git", "checkout", sha], cwd=dest)


def _local_changes(cwd: Path, sha: str) -> tuple[str, list[str]]:
    return _git_local_changes(cwd, sha)


def _apply_local_changes(
    dest: Path,
    cwd: Path,
    sha: str,
    patch: str,
    untracked: list[str],
) -> None:
    """Apply pre-computed working-tree changes into dest.

    Applies *patch* via `git apply` and copies each path in *untracked*.
    """
    if patch:
        result = subprocess.run(
            ["git", "apply", "-"],
            input=patch,
            cwd=dest,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise CloneError(
                "Failed to apply local changes to clone",
                sha=sha,
                stderr=result.stderr.strip(),
            )
        info("clone", sha=sha, step="apply_patch", lines=patch.count("\n"))

    for rel_path in untracked:
        src = cwd / rel_path
        dst = dest / rel_path
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if untracked:
        info("clone", sha=sha, step="copy_untracked", count=len(untracked))


def ensure_clone(
    sha: str,
    remote_url: str,
    cache_root: Path | None = None,
    cwd: Path | None = None,
    dirty_policy: DirtyPolicy = "ignore",
) -> CloneResult:
    """Ensure a shallow clone for sha exists in cache_root.

    Cache hit: reuse the existing directory, run ``bazel clean``.
    Cache miss: shallow-fetch + checkout from *remote_url*.  If the SHA is not
    found on the remote (e.g. a local-only commit), falls back to cloning from
    *cwd* (defaults to ``Path.cwd()``).
    Lock: O_CREAT|O_EXCL sentinel file prevents concurrent clones for the
    same SHA.

    dirty_policy controls what happens when the local CWD has changes
    relative to the pinned SHA:
      "ignore" — proceed without touching the clone (default)
      "error"  — raise CloneError if any changes are detected
      "apply"  — apply the diff and copy untracked files into the clone;
                 if a cached clone already exists it is invalidated first
                 so changes are applied to a clean base

    Returns a CloneResult with the clone path and applied-diff metadata.
    Raises CloneError on any failure.
    """
    root = cache_root if cache_root is not None else _CACHE_ROOT_DEFAULT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)

    local_source = cwd if cwd is not None else Path.cwd()
    dest = _cache_dir(root, sha)

    # Evaluate dirty state upfront — before any cache decision — so that
    # a stale cached clone does not silently hide local modifications.
    patch: str = ""
    untracked: list[str] = []
    if dirty_policy != "ignore":
        patch, untracked = _local_changes(local_source, sha)
        has_changes = bool(patch or untracked)
        if has_changes and dirty_policy == "error":
            raise CloneError(
                "Local working directory has changes relative to the pinned SHA",
                sha=sha,
                cwd=str(local_source),
                hint="Use --dirty-policy=apply to include changes or --dirty-policy=ignore to skip this check.",
            )
        if has_changes and dirty_policy == "apply" and _check_cache(root, sha):
            # The cached clone pre-dates the local changes; remove it so the
            # clone phase produces a clean base to apply the diff onto.
            info("clone", sha=sha, cache="invalidate", reason="dirty-policy=apply with local changes")
            shutil.rmtree(dest, ignore_errors=True)

    if _check_cache(root, sha):
        info("clone", sha=sha, cache="hit", dest=str(dest))
    else:
        lock = _acquire_lock(root, sha)
        try:
            if not _check_cache(root, sha):
                info("clone", sha=sha, cache="miss", dest=str(dest))
                dest.mkdir(parents=True, exist_ok=True)
                try:
                    _clone_from(remote_url, sha, dest)
                    info("clone", sha=sha, source="remote", dest=str(dest))
                except CloneError:
                    # SHA not on remote — try the local working directory.
                    shutil.rmtree(dest, ignore_errors=True)
                    dest.mkdir(parents=True, exist_ok=True)
                    info("clone", sha=sha, source="local", local_source=str(local_source))
                    try:
                        _clone_from(str(local_source), sha, dest)
                    except CloneError:
                        shutil.rmtree(dest, ignore_errors=True)
                        raise
            else:
                info("clone", sha=sha, cache="hit-after-lock", dest=str(dest))
        finally:
            _release_lock(lock)

    applied_patch = ""
    applied_untracked: list[str] = []
    if dirty_policy == "apply" and (patch or untracked):
        _apply_local_changes(dest, local_source, sha, patch, untracked)
        applied_patch = patch
        applied_untracked = untracked

    info("clone", sha=sha, step="bazel_clean", dest=str(dest))
    _run_bazel(["bazel", "clean"], cwd=dest)

    return CloneResult(
        path=dest,
        applied_patch=applied_patch,
        applied_untracked=applied_untracked,
    )
