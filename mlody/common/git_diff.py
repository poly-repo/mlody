"""Shared git diff utilities for computing local workspace changes."""

from __future__ import annotations

import subprocess
from pathlib import Path


def local_changes(cwd: Path, sha: str) -> tuple[str, list[str]]:
    """Return (patch, untracked_paths) for changes in cwd relative to sha.

    patch           — unified diff output of `git diff <sha>` (empty if none
                      or if sha is unknown locally), with untracked file diffs
                      appended via `git diff --no-index /dev/null <path>`
    untracked_paths — relative paths of untracked files (empty if none)
    """
    diff = subprocess.run(
        ["git", "diff", sha],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    patch = diff.stdout if diff.returncode == 0 else ""

    ls = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    untracked = [p for p in ls.stdout.splitlines() if p] if ls.returncode == 0 else []

    for rel_path in untracked:
        result = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", rel_path],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        # exit code 1 = differences found (normal for any non-empty new file)
        if result.returncode in (0, 1):
            patch += result.stdout

    return patch, untracked


def count_local_changes(patch: str, untracked: list[str]) -> tuple[int, int]:
    """Return (n_changed_files, n_untracked_files) from a local_changes() result."""
    n_changed = len({line for line in patch.splitlines() if line.startswith("diff ")})
    return n_changed, len(untracked)


def count_workspace_changes(cwd: Path, sha: str) -> tuple[int, int]:
    """Return (n_modified_files, n_untracked_files) relative to sha using name-only queries.

    Unlike count_local_changes, this does not build a full patch and correctly
    separates tracked-modified from untracked counts.
    """
    diff_names = subprocess.run(
        ["git", "diff", "--name-only", sha],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    n_changed = (
        len([p for p in diff_names.stdout.splitlines() if p])
        if diff_names.returncode == 0
        else 0
    )
    ls = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    n_untracked_files = (
        len([p for p in ls.stdout.splitlines() if p]) if ls.returncode == 0 else 0
    )
    return n_changed, n_untracked_files
