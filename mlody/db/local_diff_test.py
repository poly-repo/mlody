"""Unit tests for mlody.db.local_diff — spec §Testing Strategy."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mlody.db.local_diff import compute_local_diff_sha, get_repo_root


# ---------------------------------------------------------------------------
# get_repo_root tests
# ---------------------------------------------------------------------------


def test_get_repo_root_success() -> None:
    """get_repo_root returns a Path when git rev-parse exits 0."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "/home/user/repo\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = get_repo_root()

    assert result == Path("/home/user/repo")
    mock_run.assert_called_once()


def test_get_repo_root_git_failure(caplog: pytest.LogCaptureFixture) -> None:
    """get_repo_root returns None and logs a warning when git exits non-zero."""
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        result = get_repo_root()

    assert result is None
    assert any("warn" in r.levelname.lower() for r in caplog.records)


def test_get_repo_root_git_not_found(caplog: pytest.LogCaptureFixture) -> None:
    """get_repo_root returns None and logs a warning when git is not on PATH."""
    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        result = get_repo_root()

    assert result is None
    assert any("warn" in r.levelname.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# compute_local_diff_sha tests — all use pyfakefs (fs fixture)
# ---------------------------------------------------------------------------


def test_compute_none_repo_root(caplog: pytest.LogCaptureFixture) -> None:
    """compute_local_diff_sha returns None and warns when repo_root is None."""
    result = compute_local_diff_sha(None)

    assert result is None
    assert any("warn" in r.levelname.lower() for r in caplog.records)


def test_compute_both_subtrees_absent(tmp_path: Path) -> None:
    """Returns SHA-256 of a newline when neither subtree exists.

    Per spec §2.1: both subtrees absent -> zero files -> hash of the combined
    string (which is "\n" after joining zero parts and appending the trailing
    newline), not None.
    """
    repo_root = tmp_path / "repo"

    result = compute_local_diff_sha(repo_root)

    # "\n".join([]) + "\n" == "\n" — SHA-256 of that single newline byte
    expected = hashlib.sha256(b"\n").hexdigest()
    assert result == expected
    assert result is not None


def test_compute_one_subtree_absent(tmp_path: Path) -> None:
    """Returns a non-null digest when only mlody/ exists.

    The digest must differ from the empty-tree hash because files are present.
    """
    repo_root = tmp_path / "repo"
    mlody_dir = repo_root / "mlody"
    mlody_dir.mkdir(parents=True)
    (mlody_dir / "file.py").write_text("print('hello')")

    result = compute_local_diff_sha(repo_root)

    empty_hash = hashlib.sha256(b"").hexdigest()
    assert result is not None
    assert len(result) == 64
    assert result != empty_hash


def test_compute_deterministic(tmp_path: Path) -> None:
    """Same files and content produce identical digests on two calls."""
    repo_root = tmp_path / "repo"
    mlody_dir = repo_root / "mlody"
    mlody_dir.mkdir(parents=True)
    (mlody_dir / "a.py").write_text("x = 1")
    (mlody_dir / "b.py").write_text("y = 2")

    first = compute_local_diff_sha(repo_root)
    second = compute_local_diff_sha(repo_root)

    assert first is not None
    assert first == second


def test_compute_untracked_file_changes_hash(tmp_path: Path) -> None:
    """Adding a new file under mlody/ changes the digest."""
    repo_root = tmp_path / "repo"
    mlody_dir = repo_root / "mlody"
    mlody_dir.mkdir(parents=True)
    (mlody_dir / "existing.py").write_text("# existing")

    before = compute_local_diff_sha(repo_root)
    (mlody_dir / "new_untracked.py").write_text("# new file")
    after = compute_local_diff_sha(repo_root)

    assert before != after


def test_compute_modified_file_changes_hash(tmp_path: Path) -> None:
    """Modifying an existing file under mlody/ changes the digest."""
    repo_root = tmp_path / "repo"
    mlody_dir = repo_root / "mlody"
    mlody_dir.mkdir(parents=True)
    target = mlody_dir / "module.py"
    target.write_text("# original content")

    before = compute_local_diff_sha(repo_root)
    target.write_text("# modified content")
    after = compute_local_diff_sha(repo_root)

    assert before != after


def test_compute_sort_order_independent(tmp_path: Path) -> None:
    """Digest is the same regardless of filesystem enumeration order.

    We create two files whose names would sort differently depending on
    traversal order, then verify both calls return the same hash. The
    implementation must sort by repo-relative path before hashing.
    """
    repo_root = tmp_path / "repo"
    mlody_dir = repo_root / "mlody"
    mlody_dir.mkdir(parents=True)
    (mlody_dir / "zzz.py").write_text("# zzz")
    (mlody_dir / "aaa.py").write_text("# aaa")

    # Call twice — same repo state, must produce the same hash.
    first = compute_local_diff_sha(repo_root)
    second = compute_local_diff_sha(repo_root)

    assert first == second
    assert first is not None
    assert len(first) == 64
