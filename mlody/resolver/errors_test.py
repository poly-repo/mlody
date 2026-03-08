"""Tests for mlody.resolver.errors — typed exception hierarchy."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlody.resolver.errors import (
    AmbiguousRefError,
    BranchTagCollisionError,
    CorruptCacheError,
    GitNetworkError,
    LabelParseError,
    LockBusyError,
    UnknownRefError,
    WorkspaceResolutionError,
)


class TestWorkspaceResolutionErrorHierarchy:
    """Requirement: Typed exception hierarchy — all subclass WorkspaceResolutionError."""

    def test_label_parse_error_is_subclass(self) -> None:
        assert issubclass(LabelParseError, WorkspaceResolutionError)

    def test_unknown_ref_error_is_subclass(self) -> None:
        assert issubclass(UnknownRefError, WorkspaceResolutionError)

    def test_ambiguous_ref_error_is_subclass(self) -> None:
        assert issubclass(AmbiguousRefError, WorkspaceResolutionError)

    def test_branch_tag_collision_error_is_subclass(self) -> None:
        assert issubclass(BranchTagCollisionError, WorkspaceResolutionError)

    def test_corrupt_cache_error_is_subclass(self) -> None:
        assert issubclass(CorruptCacheError, WorkspaceResolutionError)

    def test_lock_busy_error_is_subclass(self) -> None:
        assert issubclass(LockBusyError, WorkspaceResolutionError)

    def test_git_network_error_is_subclass(self) -> None:
        assert issubclass(GitNetworkError, WorkspaceResolutionError)

    def test_workspace_resolution_error_is_exception(self) -> None:
        assert issubclass(WorkspaceResolutionError, Exception)


class TestLabelParseError:
    """Requirement: LabelParseError carries label and reason fields."""

    def test_fields_set_correctly(self) -> None:
        exc = LabelParseError(label="bad-label", reason="missing '|' separator")
        assert exc.label == "bad-label"
        assert exc.reason == "missing '|' separator"

    def test_str_contains_label(self) -> None:
        exc = LabelParseError(label="bad-label", reason="missing '|' separator")
        assert "bad-label" in str(exc)

    def test_raises_correctly(self) -> None:
        with pytest.raises(LabelParseError) as exc_info:
            raise LabelParseError("x", "reason")
        assert exc_info.value.label == "x"


class TestUnknownRefError:
    """Requirement: UnknownRefError carries committoid and remote fields."""

    def test_fields_set_correctly(self) -> None:
        exc = UnknownRefError(committoid="nosuchbranch", remote="origin")
        assert exc.committoid == "nosuchbranch"
        assert exc.remote == "origin"

    def test_str_contains_committoid(self) -> None:
        exc = UnknownRefError("nosuchbranch", "origin")
        assert "nosuchbranch" in str(exc)


class TestAmbiguousRefError:
    """Requirement: AmbiguousRefError carries committoid and matching_shas fields."""

    def test_fields_set_correctly(self) -> None:
        shas = ["aaa111", "bbb222"]
        exc = AmbiguousRefError(committoid="abc", matching_shas=shas)
        assert exc.committoid == "abc"
        assert exc.matching_shas == shas

    def test_str_contains_committoid_and_shas(self) -> None:
        exc = AmbiguousRefError("abc", ["aaa111", "bbb222"])
        msg = str(exc)
        assert "abc" in msg
        assert "aaa111" in msg


class TestBranchTagCollisionError:
    """Requirement: BranchTagCollisionError carries name, head_sha, tag_sha fields."""

    def test_fields_set_correctly(self) -> None:
        exc = BranchTagCollisionError(name="v1.0", head_sha="aaa", tag_sha="bbb")
        assert exc.name == "v1.0"
        assert exc.head_sha == "aaa"
        assert exc.tag_sha == "bbb"

    def test_str_contains_name_and_both_shas(self) -> None:
        exc = BranchTagCollisionError("v1.0", "aaa", "bbb")
        msg = str(exc)
        assert "v1.0" in msg
        assert "refs/heads/v1.0" in msg
        assert "refs/tags/v1.0" in msg


class TestCorruptCacheError:
    """Requirement: CorruptCacheError carries cache_dir field."""

    def test_fields_set_correctly(self) -> None:
        d = Path("/some/cache/dir")
        exc = CorruptCacheError(cache_dir=d)
        assert exc.cache_dir == d

    def test_str_contains_path(self) -> None:
        d = Path("/some/cache/dir")
        exc = CorruptCacheError(d)
        assert "/some/cache/dir" in str(exc)


class TestLockBusyError:
    """Requirement: LockBusyError carries lock_path field."""

    def test_fields_set_correctly(self) -> None:
        p = Path("/some/sha.lock")
        exc = LockBusyError(lock_path=p)
        assert exc.lock_path == p

    def test_str_contains_path(self) -> None:
        p = Path("/some/sha.lock")
        exc = LockBusyError(p)
        assert "/some/sha.lock" in str(exc)


class TestGitNetworkError:
    """Requirement: GitNetworkError carries command, stderr, and returncode fields."""

    def test_fields_set_correctly(self) -> None:
        cmd = ["git", "ls-remote", "origin"]
        exc = GitNetworkError(command=cmd, stderr="connection refused", returncode=128)
        assert exc.command == cmd
        assert exc.stderr == "connection refused"
        assert exc.returncode == 128

    def test_str_contains_command_and_stderr(self) -> None:
        exc = GitNetworkError(["git", "ls-remote"], "connection refused", 128)
        msg = str(exc)
        assert "git" in msg
        assert "connection refused" in msg
        assert "128" in msg
