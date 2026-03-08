"""Typed exception hierarchy for workspace resolution failures."""

from __future__ import annotations

from pathlib import Path


class WorkspaceResolutionError(Exception):
    """Base class for all workspace resolution errors."""


class LabelParseError(WorkspaceResolutionError):
    """The label string has invalid syntax."""

    def __init__(self, label: str, reason: str) -> None:
        self.label = label
        self.reason = reason
        super().__init__(f"Cannot parse label {label!r}: {reason}")


class UnknownRefError(WorkspaceResolutionError):
    """The committoid was not found on the remote."""

    def __init__(self, committoid: str, remote: str) -> None:
        self.committoid = committoid
        self.remote = remote
        super().__init__(f"Unknown ref {committoid!r} on remote {remote!r}")


class AmbiguousRefError(WorkspaceResolutionError):
    """A short SHA prefix matches more than one remote SHA."""

    def __init__(self, committoid: str, matching_shas: list[str]) -> None:
        self.committoid = committoid
        self.matching_shas = matching_shas
        super().__init__(
            f"Ambiguous ref {committoid!r} matches {len(matching_shas)} SHAs: "
            f"{', '.join(matching_shas)}. Provide a longer prefix."
        )


class BranchTagCollisionError(WorkspaceResolutionError):
    """A name exists as both a branch and a tag on origin."""

    def __init__(self, name: str, head_sha: str, tag_sha: str) -> None:
        self.name = name
        self.head_sha = head_sha
        self.tag_sha = tag_sha
        super().__init__(
            f"Name {name!r} exists as both refs/heads/{name} (SHA {head_sha}) "
            f"and refs/tags/{name} (SHA {tag_sha}). Be explicit."
        )


class CorruptCacheError(WorkspaceResolutionError):
    """A cache directory exists but the sentinel file is missing."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        super().__init__(
            f"Cache directory {cache_dir} exists but is missing the sentinel "
            f"mlody/roots.mlody. Delete the directory and retry."
        )


class LockBusyError(WorkspaceResolutionError):
    """Another process holds the materialisation lock."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(
            f"Lock file {lock_path} already exists. Another process may be "
            f"materialising this workspace. If the previous process has exited, "
            f"delete the lock file and retry."
        )


class NoMlodyAtCommitError(WorkspaceResolutionError):
    """The resolved commit predates mlody (no mlody/roots.mlody present)."""

    def __init__(self, committoid: str, sha: str) -> None:
        self.committoid = committoid
        self.sha = sha
        super().__init__(
            f"No mlody/roots.mlody found at commit {sha[:12]} "
            f"(requested {committoid!r}). That commit may predate mlody."
        )


class GitNetworkError(WorkspaceResolutionError):
    """A git subprocess exited with a non-zero return code."""

    def __init__(self, command: list[str], stderr: str, returncode: int) -> None:
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(
            f"git command failed (exit {returncode}): {' '.join(command)}\n{stderr}"
        )
