"""Tests for mlody.resolver.git_client — GitClient subprocess abstraction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from mlody.resolver.errors import GitNetworkError
from mlody.resolver.git_client import GitClient


# Helpers for building fake subprocess.CompletedProcess results
def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = stderr
    return result


def _fail(stderr: str = "git error", returncode: int = 128) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = ""
    result.stderr = stderr
    return result


class TestLsRemote:
    """Requirement: GitClient.ls_remote parses ref map from git ls-remote."""

    def test_returns_sha_ref_pairs(self, tmp_path: Path) -> None:
        stdout = (
            "abc1234567890123456789012345678901234567\trefs/heads/main\n"
            "def1234567890123456789012345678901234567\trefs/heads/feature\n"
        )
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok(stdout)) as mock_run:
            pairs = client.ls_remote()

        assert pairs == [
            ("abc1234567890123456789012345678901234567", "refs/heads/main"),
            ("def1234567890123456789012345678901234567", "refs/heads/feature"),
        ]
        # Verify no shell=True
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert not kwargs.get("shell", False)

    def test_parses_annotated_tag_deref_entry(self, tmp_path: Path) -> None:
        # ^{} suffix is the dereferenced commit SHA for annotated tags
        stdout = (
            "aaa0000000000000000000000000000000000000\trefs/tags/v1.0.0\n"
            "bbb0000000000000000000000000000000000000\trefs/tags/v1.0.0^{}\n"
        )
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok(stdout)):
            pairs = client.ls_remote()

        assert ("aaa0000000000000000000000000000000000000", "refs/tags/v1.0.0") in pairs
        assert ("bbb0000000000000000000000000000000000000", "refs/tags/v1.0.0^{}") in pairs

    def test_raises_git_network_error_on_non_zero_exit(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_fail("connection refused", 128)):
            with pytest.raises(GitNetworkError) as exc_info:
                client.ls_remote()

        assert exc_info.value.returncode == 128
        assert "connection refused" in exc_info.value.stderr

    def test_empty_output_returns_empty_list(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("")):
            pairs = client.ls_remote()

        assert pairs == []

    def test_command_uses_list_not_shell_string(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("")) as mock_run:
            client.ls_remote()

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert isinstance(cmd, list)
        assert cmd == ["git", "ls-remote", "origin"]
        assert not kwargs.get("shell", False)


class TestCatFileType:
    """Requirement: GitClient.cat_file_type returns type string or None."""

    def test_returns_commit_when_present(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("commit\n")):
            result = client.cat_file_type("abc1234")

        assert result == "commit"

    def test_returns_none_on_non_zero_exit(self, tmp_path: Path) -> None:
        # Non-zero exit means the object is absent — this is not an error condition
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_fail("", 128)):
            result = client.cat_file_type("unknown-sha")

        assert result is None

    def test_command_passes_sha_as_list_element(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("commit")) as mock_run:
            client.cat_file_type("deadbeef")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == ["git", "cat-file", "-t", "deadbeef"]
        assert not kwargs.get("shell", False)


class TestCloneLocal:
    """Requirement: GitClient.clone_local uses file:// transport, no network."""

    def test_runs_clone_and_checkout(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        sha = "abc" * 13 + "a"  # 40-char SHA
        client = GitClient(tmp_path)

        with patch("subprocess.run", return_value=_ok("")) as mock_run:
            client.clone_local(dest, sha)

        calls = mock_run.call_args_list
        # First call: clone
        clone_cmd = calls[0].args[0]
        assert "clone" in clone_cmd
        assert "--local" in clone_cmd
        assert "--no-checkout" in clone_cmd
        assert f"file:///{tmp_path}" in clone_cmd
        assert str(dest) in clone_cmd

    def test_raises_git_network_error_when_clone_fails(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        client = GitClient(tmp_path)

        with patch("subprocess.run", return_value=_fail("clone failed", 128)):
            with pytest.raises(GitNetworkError):
                client.clone_local(dest, "deadbeef")

    def test_no_shell_interpolation(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        client = GitClient(tmp_path)

        with patch("subprocess.run", return_value=_ok("")) as mock_run:
            client.clone_local(dest, "deadbeef")

        for c in mock_run.call_args_list:
            _, kwargs = c
            assert not kwargs.get("shell", False)
            assert isinstance(c.args[0], list)


class TestCloneRemote:
    """Requirement: GitClient.clone_remote uses blob:none filter for minimal transfer."""

    def test_runs_clone_fetch_checkout(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        sha = "abc" * 13 + "a"
        client = GitClient(tmp_path)

        with patch("subprocess.run", return_value=_ok("")) as mock_run:
            client.clone_remote(dest, sha)

        calls = mock_run.call_args_list
        clone_cmd = calls[0].args[0]
        assert "clone" in clone_cmd
        assert "--filter=blob:none" in clone_cmd
        assert "--no-checkout" in clone_cmd
        assert "origin" in clone_cmd
        assert str(dest) in clone_cmd

    def test_raises_git_network_error_on_failure(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        client = GitClient(tmp_path)

        with patch("subprocess.run", return_value=_fail("remote error", 128)):
            with pytest.raises(GitNetworkError):
                client.clone_remote(dest, "deadbeef")


class TestRemoteUrl:
    """Requirement: GitClient.remote_url returns origin URL."""

    def test_returns_stripped_url(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("git@github.com:org/repo.git\n")):
            url = client.remote_url()

        assert url == "git@github.com:org/repo.git"

    def test_raises_git_network_error_on_failure(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_fail("no remote", 2)):
            with pytest.raises(GitNetworkError) as exc_info:
                client.remote_url()

        assert exc_info.value.returncode == 2

    def test_command_is_correct(self, tmp_path: Path) -> None:
        client = GitClient(tmp_path)
        with patch("subprocess.run", return_value=_ok("url")) as mock_run:
            client.remote_url()

        args, kwargs = mock_run.call_args
        assert args[0] == ["git", "remote", "get-url", "origin"]
        assert not kwargs.get("shell", False)
