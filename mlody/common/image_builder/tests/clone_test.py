"""Tests for phases/clone.py — cache/clone/lock logic using pyfakefs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import mlody
import pytest
from pyfakefs import fake_filesystem as _fake_filesystem

from mlody.common.image_builder.errors import CloneError
from mlody.common.image_builder.phases.clone import (
    _acquire_lock,
    _cache_dir,
    _check_cache,
    _lock_path,
    ensure_clone,
)

assert mlody.__name__ == "mlody"
assert _fake_filesystem.__name__ == "pyfakefs.fake_filesystem"

_SHA = "a" * 40
_REMOTE = "https://example.com/repo.git"
_CACHE_ROOT = Path("/fake/cache")
_CLONE_DEST = _CACHE_ROOT / _SHA


def _make_valid_clone(fs: object, cache_root: Path, sha: str) -> None:
    """Simulate a completed clone by creating the .git/HEAD sentinel."""
    dest = cache_root / sha
    fs.create_file(str(dest / ".git" / "HEAD"), contents="ref: refs/heads/main\n")  # type: ignore[attr-defined]


class TestCheckCache:
    def test_returns_false_when_directory_absent(self, fs: object) -> None:
        assert _check_cache(_CACHE_ROOT, _SHA) is False

    def test_returns_false_when_directory_exists_but_no_git_head(
        self, fs: object
    ) -> None:
        _CLONE_DEST.mkdir(parents=True)
        assert _check_cache(_CACHE_ROOT, _SHA) is False

    def test_returns_true_when_git_head_exists(self, fs: object) -> None:
        (_CLONE_DEST / ".git").mkdir(parents=True)
        (_CLONE_DEST / ".git" / "HEAD").write_text("ref: refs/heads/main")
        assert _check_cache(_CACHE_ROOT, _SHA) is True


class TestAcquireLock:
    def test_creates_lock_file(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        lock = _acquire_lock(_CACHE_ROOT, _SHA)
        assert lock.exists()

    def test_raises_clone_error_on_contention(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        # Simulate an existing lock (another process already acquired it)
        _lock_path(_CACHE_ROOT, _SHA).write_text("locked")
        with pytest.raises(CloneError) as exc_info:
            _acquire_lock(_CACHE_ROOT, _SHA)
        assert "lock" in exc_info.value.context


class TestEnsureClone:
    def test_cache_miss_invokes_git_commands_and_bazel_clean(
        self, fs: object
    ) -> None:
        _CACHE_ROOT.mkdir(parents=True)

        bazel_calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            # After git checkout, simulate the .git/HEAD sentinel appearing
            if args[:3] == ["git", "checkout", _SHA]:
                dest = _cache_dir(_CACHE_ROOT, _SHA)
                git_dir = dest / ".git"
                git_dir.mkdir(parents=True, exist_ok=True)
                (git_dir / "HEAD").write_text("ref: refs/heads/main")
            if args[0] == "bazel":
                bazel_calls.append(args)
            return mock

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT)

        assert result.path == _CACHE_ROOT / _SHA
        assert ["bazel", "clean"] in bazel_calls

    def test_cache_hit_skips_git_clone_calls(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        # Pre-populate a valid clone
        (_CLONE_DEST / ".git").mkdir(parents=True)
        (_CLONE_DEST / ".git" / "HEAD").write_text("ref: refs/heads/main")

        git_calls: list[list[str]] = []
        bazel_calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            if args[0] == "git":
                git_calls.append(args)
            if args[0] == "bazel":
                bazel_calls.append(args)
            return mock

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT)

        # On cache hit, no git commands should run
        assert git_calls == []
        # bazel clean must always run
        assert ["bazel", "clean"] in bazel_calls
        assert result.path == _CLONE_DEST

    def test_partial_directory_cleaned_up_on_clone_failure(
        self, fs: object
    ) -> None:
        _CACHE_ROOT.mkdir(parents=True)

        call_count = 0

        def fake_run(args: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            # First git command (clone) creates the dest dir
            if args[0] == "git" and "--no-checkout" in args:
                _CLONE_DEST.mkdir(parents=True, exist_ok=True)
            # Second git command (fetch) fails
            if args[0] == "git" and "--depth" in args:
                mock.returncode = 1
                mock.stderr = "fatal: shallow fetch error"
            return mock

        with pytest.raises(CloneError):
            with patch(
                "mlody.common.image_builder.phases.clone.subprocess.run",
                side_effect=fake_run,
            ):
                ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT)

        # The partial destination directory must be removed after failure
        assert not _CLONE_DEST.exists()

    def test_falls_back_to_local_cwd_when_remote_fetch_fails(
        self, fs: object
    ) -> None:
        """If the remote fetch fails, retry cloning from the local cwd."""
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")

        remote_attempted: list[str] = []
        local_attempted: list[str] = []

        def fake_run(args: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            if args[0] == "git" and "--no-checkout" in args:
                source = args[-2]
                if source == _REMOTE:
                    remote_attempted.append(source)
                    mock.returncode = 1
                    mock.stderr = "fatal: repository not found"
                else:
                    local_attempted.append(source)
            if args[:3] == ["git", "checkout", _SHA]:
                dest = _cache_dir(_CACHE_ROOT, _SHA)
                git_dir = dest / ".git"
                git_dir.mkdir(parents=True, exist_ok=True)
                (git_dir / "HEAD").write_text("ref: refs/heads/main")
            return mock

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd)

        assert result.path == _CLONE_DEST
        assert _REMOTE in remote_attempted
        assert str(local_cwd) in local_attempted

    def test_lock_contention_raises_clone_error(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        # Simulate an existing lock file from another process
        _lock_path(_CACHE_ROOT, _SHA).write_text("locked by other process")

        with pytest.raises(CloneError) as exc_info:
            ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT)

        assert "lock" in exc_info.value.context


class TestDirtyPolicy:
    """Tests for dirty_policy behaviour on local CWD fallback."""

    def _fake_run_local_fallback(
        self,
        sha: str,
        remote: str,
        local_cwd: "Path",
        patch_output: str = "",
        untracked_output: str = "",
    ):
        """Return a fake subprocess.run that:
        - fails the remote clone (triggering local fallback)
        - succeeds the local clone (creating .git/HEAD)
        - returns patch/untracked data for the dirty checks
        - succeeds bazel clean
        """
        from unittest.mock import MagicMock
        from mlody.common.image_builder.phases.clone import _cache_dir

        def fake_run(args: list, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            # Remote clone fails
            if args[0] == "git" and "--no-checkout" in args and remote in args:
                mock.returncode = 1
                mock.stderr = "fatal: repository not found"
                return mock
            # Local clone succeeds — create sentinel
            if args[0] == "git" and "--no-checkout" in args and str(local_cwd) in args:
                dest = _cache_dir(_CACHE_ROOT, sha)
                git_dir = dest / ".git"
                git_dir.mkdir(parents=True, exist_ok=True)
                (git_dir / "HEAD").write_text("ref: refs/heads/main")
                return mock
            # git diff <sha> → return patch
            if args[:2] == ["git", "diff"] and args[2] == sha:
                mock.stdout = patch_output
                return mock
            # git ls-files --others
            if args[:2] == ["git", "ls-files"]:
                mock.stdout = untracked_output
                return mock
            # git apply - → apply patch
            if args[:2] == ["git", "apply"]:
                return mock
            # git fetch / checkout / bazel
            return mock

        return fake_run

    def test_dirty_policy_ignore_does_not_check_changes(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")

        diff_called: list[bool] = []

        def fake_run(args: list, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            if args[:2] == ["git", "diff"]:
                diff_called.append(True)
            if args[0] == "git" and "--no-checkout" in args and str(local_cwd) in args:
                dest = _cache_dir(_CACHE_ROOT, _SHA)
                (dest / ".git").mkdir(parents=True, exist_ok=True)
                (dest / ".git" / "HEAD").write_text("ref: refs/heads/main")
            if args[0] == "git" and "--no-checkout" in args and _REMOTE in args:
                mock.returncode = 1
            return mock

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd, dirty_policy="ignore")

        assert not diff_called

    def test_dirty_policy_error_raises_when_changes_present(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")

        fake_run = self._fake_run_local_fallback(
            _SHA, _REMOTE, local_cwd, patch_output="diff --git a/foo.py b/foo.py\n"
        )

        with pytest.raises(CloneError) as exc_info:
            with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
                ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd, dirty_policy="error")

        assert "changes" in exc_info.value.message.lower()

    def test_dirty_policy_error_passes_when_no_changes(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")

        fake_run = self._fake_run_local_fallback(_SHA, _REMOTE, local_cwd)

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd, dirty_policy="error")

        assert result.path == _CLONE_DEST

    def test_dirty_policy_apply_calls_git_apply(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")

        apply_calls: list[list[str]] = []
        patch_data = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"

        base_fake_run = self._fake_run_local_fallback(
            _SHA, _REMOTE, local_cwd, patch_output=patch_data
        )

        def fake_run(args: list, **kwargs):
            if args[:2] == ["git", "apply"]:
                apply_calls.append(args)
                mock = MagicMock()
                mock.returncode = 0
                mock.stdout = ""
                mock.stderr = ""
                return mock
            return base_fake_run(args, **kwargs)

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd, dirty_policy="apply")

        assert result.path == _CLONE_DEST
        assert any("apply" in a for a in apply_calls)

    def test_dirty_policy_apply_copies_untracked_files(self, fs: object) -> None:
        _CACHE_ROOT.mkdir(parents=True)
        local_cwd = Path("/local/repo")
        local_cwd.mkdir(parents=True)
        # Create an untracked file in the fake CWD
        (local_cwd / "new_file.py").write_text("# new")

        fake_run = self._fake_run_local_fallback(
            _SHA, _REMOTE, local_cwd, untracked_output="new_file.py\n"
        )

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            result = ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, cwd=local_cwd, dirty_policy="apply")

        assert (result.path / "new_file.py").exists()

    def test_dirty_policy_error_raises_on_cache_hit_with_changes(self, fs: object) -> None:
        """error policy must fire even when the clone is already cached."""
        _CACHE_ROOT.mkdir(parents=True)
        # Pre-populate a valid cached clone
        (_CLONE_DEST / ".git").mkdir(parents=True)
        (_CLONE_DEST / ".git" / "HEAD").write_text("ref: refs/heads/main")

        def fake_run(args: list, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            if args[:2] == ["git", "diff"]:
                mock.stdout = "diff --git a/foo.py b/foo.py\n"
            if args[:2] == ["git", "ls-files"]:
                mock.stdout = ""
            return mock

        with pytest.raises(CloneError) as exc_info:
            with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
                ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, dirty_policy="error")

        assert "changes" in exc_info.value.message.lower()

    def test_dirty_policy_apply_invalidates_cache_when_changes_present(self, fs: object) -> None:
        """apply policy must discard a stale cached clone and re-clone."""
        _CACHE_ROOT.mkdir(parents=True)
        # Pre-populate a valid cached clone
        (_CLONE_DEST / ".git").mkdir(parents=True)
        (_CLONE_DEST / ".git" / "HEAD").write_text("ref: refs/heads/main")

        clone_called: list[bool] = []

        def fake_run(args: list, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            if args[:2] == ["git", "diff"]:
                mock.stdout = "diff --git a/foo.py b/foo.py\n"
            if args[:2] == ["git", "ls-files"]:
                mock.stdout = ""
            if args[0] == "git" and "--no-checkout" in args:
                clone_called.append(True)
                # Recreate the sentinel so cache check passes after re-clone
                (_CLONE_DEST / ".git").mkdir(parents=True, exist_ok=True)
                (_CLONE_DEST / ".git" / "HEAD").write_text("ref: refs/heads/main")
            if args[:2] == ["git", "apply"]:
                pass  # apply succeeds
            return mock

        with patch("mlody.common.image_builder.phases.clone.subprocess.run", side_effect=fake_run):
            ensure_clone(_SHA, _REMOTE, cache_root=_CACHE_ROOT, dirty_policy="apply")

        # The cached clone was invalidated, so a fresh clone must have run
        assert clone_called
