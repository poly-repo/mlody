"""Tests for mlody.resolver.resolver — label parsing, SHA resolution, and factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mlody.resolver.errors import (
    AmbiguousRefError,
    BranchTagCollisionError,
    LabelParseError,
    LockBusyError,
    UnknownRefError,
    WorkspaceResolutionError,
)
from mlody.resolver.resolver import parse_label, resolve_sha, resolve_workspace

SHA_MAIN = "a" * 40
SHA_TAG = "b" * 40
SHA_ALT = "c" * 40


# ---------------------------------------------------------------------------
# parse_label
# ---------------------------------------------------------------------------


class TestParseLabel:
    """Requirement: Extended label syntax parsing."""

    def test_at_prefix_passthrough(self) -> None:
        # Scenario: cwd label passthrough — @-prefixed
        committoid, inner = parse_label("@lexica//models:bert")
        assert committoid is None
        assert inner == "@lexica//models:bert"

    def test_double_slash_prefix_passthrough(self) -> None:
        # Scenario: cwd label passthrough — //-prefixed
        committoid, inner = parse_label("//models:bert")
        assert committoid is None
        assert inner == "//models:bert"

    def test_branch_name_committoid(self) -> None:
        # Scenario: committoid-qualified label — branch name
        committoid, inner = parse_label("main|@lexica//models:bert")
        assert committoid == "main"
        assert inner == "@lexica//models:bert"

    def test_tag_name_committoid(self) -> None:
        # Scenario: committoid-qualified label — tag name
        committoid, inner = parse_label("v1.2.0|//models:bert")
        assert committoid == "v1.2.0"
        assert inner == "//models:bert"

    def test_short_sha_committoid(self) -> None:
        # Scenario: committoid-qualified label — short SHA
        committoid, inner = parse_label("abc1234|@lexica//models:bert")
        assert committoid == "abc1234"
        assert inner == "@lexica//models:bert"

    def test_missing_pipe_raises_label_parse_error(self) -> None:
        # Scenario: missing '|' with non-@// prefix raises LabelParseError
        with pytest.raises(LabelParseError) as exc_info:
            parse_label("notaref")
        assert exc_info.value.label == "notaref"
        assert "|" in exc_info.value.reason or "separator" in exc_info.value.reason

    def test_inner_label_not_at_or_slash_raises_label_parse_error(self) -> None:
        # Scenario: inner label does not start with @ or // raises LabelParseError
        with pytest.raises(LabelParseError) as exc_info:
            parse_label("main|notaninnerref")
        assert exc_info.value.label == "main|notaninnerref"
        assert "notaninnerref" in exc_info.value.reason


# ---------------------------------------------------------------------------
# resolve_sha
# ---------------------------------------------------------------------------


def _make_git_client(pairs: list[tuple[str, str]]) -> MagicMock:
    client = MagicMock()
    client.ls_remote.return_value = pairs
    return client


class TestResolveSha:
    """Requirement: Committoid resolution via git ls-remote."""

    def test_branch_resolves_to_sha(self) -> None:
        # Scenario: branch name resolves to full SHA
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/main"),
            (SHA_TAG, "refs/heads/other"),
        ])
        result = resolve_sha("main", client)
        assert result == SHA_MAIN

    def test_lightweight_tag_resolves(self) -> None:
        # Scenario: exact tag name resolves — lightweight tag
        client = _make_git_client([
            (SHA_TAG, "refs/tags/v1.0.0"),
        ])
        result = resolve_sha("v1.0.0", client)
        assert result == SHA_TAG

    def test_annotated_tag_prefers_deref_sha(self) -> None:
        # Scenario: annotated tag prefers ^{} entry
        tag_obj_sha = "a" * 40
        commit_sha = "b" * 40
        client = _make_git_client([
            (tag_obj_sha, "refs/tags/v1.0.0"),
            (commit_sha, "refs/tags/v1.0.0^{}"),
        ])
        result = resolve_sha("v1.0.0", client)
        assert result == commit_sha

    def test_short_sha_resolves_unique_prefix(self) -> None:
        # Scenario: short SHA resolves when exactly one remote SHA matches prefix
        full_sha = "abc1234" + "0" * 33
        other_sha = "def5678" + "0" * 33
        client = _make_git_client([
            (full_sha, "refs/heads/main"),
            (other_sha, "refs/heads/feature"),
        ])
        result = resolve_sha("abc1234", client)
        assert result == full_sha

    def test_unknown_ref_raises(self) -> None:
        # Scenario: unknown ref raises UnknownRefError
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/main"),
        ])
        with pytest.raises(UnknownRefError) as exc_info:
            resolve_sha("nosuchbranch", client)
        assert exc_info.value.committoid == "nosuchbranch"
        assert exc_info.value.remote == "origin"

    def test_ambiguous_short_sha_raises(self) -> None:
        # Scenario: ambiguous short SHA raises AmbiguousRefError
        sha1 = "abc1234" + "0" * 33
        sha2 = "abc1234" + "1" * 33
        client = _make_git_client([
            (sha1, "refs/heads/main"),
            (sha2, "refs/heads/feature"),
        ])
        with pytest.raises(AmbiguousRefError) as exc_info:
            resolve_sha("abc1234", client)
        assert exc_info.value.committoid == "abc1234"
        assert sha1 in exc_info.value.matching_shas
        assert sha2 in exc_info.value.matching_shas

    def test_branch_tag_collision_raises(self) -> None:
        # Scenario: branch and tag share the same name raises BranchTagCollisionError
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/v1.0"),
            (SHA_TAG, "refs/tags/v1.0"),
        ])
        with pytest.raises(BranchTagCollisionError) as exc_info:
            resolve_sha("v1.0", client)
        assert exc_info.value.name == "v1.0"


# ---------------------------------------------------------------------------
# resolve_workspace — factory
# ---------------------------------------------------------------------------


class TestResolveWorkspaceCwdPath:
    """Requirement: resolve_workspace cwd passthrough."""

    def test_cwd_label_returns_monorepo_workspace_and_none_sha(
        self, tmp_path: Path
    ) -> None:
        # Scenario: cwd path — label starts with @
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace("@lexica//models:bert", monorepo_root=tmp_path)

        assert sha is None
        assert ws is mock_ws
        mock_ws_cls.assert_called_once_with(
            monorepo_root=tmp_path,
            roots_file=None,
            print_fn=print,
        )
        mock_ws.load.assert_called_once()

    def test_double_slash_label_returns_cwd_workspace(self, tmp_path: Path) -> None:
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace("//models:bert", monorepo_root=tmp_path)

        assert sha is None


class TestResolveWorkspaceCommittoidPath:
    """Requirement: resolve_workspace committoid path — branch cache miss."""

    def _make_fake_client(self, full_sha: str) -> MagicMock:
        client = MagicMock()
        client.ls_remote.return_value = [(full_sha, "refs/heads/main")]
        client.cat_file_type.return_value = "commit"
        client.remote_url.return_value = "git@github.com:org/repo.git"
        return client

    def test_returns_workspace_and_sha_on_cache_miss(self, tmp_path: Path) -> None:
        # Scenario: branch name, cache miss → materialise → workspace returned
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha
        assert ws is mock_ws
        # Workspace constructed from the cache dir
        dest = cache_root / full_sha
        mock_ws_cls.assert_called_once_with(
            monorepo_root=dest,
            roots_file=None,
            print_fn=print,
        )
        mock_ws.load.assert_called_once()

    def test_cache_hit_skips_clone(self, tmp_path: Path) -> None:
        # Scenario: committoid path — cache hit skips cloning
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        # Pre-create the sentinel so check_cache returns "hit"
        sentinel = cache_root / full_sha / "mlody" / "roots.mlody"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()

        client = self._make_fake_client(full_sha)

        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha
        # Clone methods must NOT be called on cache hit
        client.clone_local.assert_not_called()
        client.clone_remote.assert_not_called()

    def test_resolver_exceptions_propagate_unchanged(self, tmp_path: Path) -> None:
        # Scenario: all resolver exceptions propagate to caller
        cache_root = tmp_path / "cache"
        client = MagicMock()
        client.ls_remote.return_value = []  # nothing → UnknownRefError

        with pytest.raises(UnknownRefError):
            resolve_workspace(
                "nosuchbranch|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

    def test_resolved_sha_is_none_for_cwd_path(self, tmp_path: Path) -> None:
        # Scenario: resolved_sha=None on cwd path
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            _, sha = resolve_workspace("@bert//:lr", monorepo_root=tmp_path)

        assert sha is None

    def test_resolved_sha_is_full_sha_on_committoid_path(self, tmp_path: Path) -> None:
        # Scenario: resolved_sha=<sha> on committoid path
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            _, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha
