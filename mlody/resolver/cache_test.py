"""Tests for mlody.resolver.cache — cache hit/miss, locking, metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlody.resolver.cache import (
    acquire_lock,
    cache_dir,
    check_cache,
    ensure_cache_root,
    release_lock,
    write_metadata,
)
from mlody.resolver.errors import CorruptCacheError, LockBusyError

SHA = "a" * 40  # 40-char fake SHA for all tests


class TestCacheDir:
    """Requirement: cache_dir returns cache_root / sha."""

    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = cache_dir(tmp_path, SHA)
        assert result == tmp_path / SHA


class TestCheckCache:
    """Requirement: Cache hit detection."""

    def test_hit_when_sentinel_present(self, tmp_path: Path) -> None:
        d = tmp_path / SHA
        sentinel = d / "mlody" / "roots.mlody"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()

        result = check_cache(tmp_path, SHA)

        assert result == "hit"

    def test_miss_when_directory_absent(self, tmp_path: Path) -> None:
        result = check_cache(tmp_path, SHA)
        assert result == "miss"

    def test_corrupt_when_directory_exists_but_sentinel_absent(
        self, tmp_path: Path
    ) -> None:
        d = tmp_path / SHA
        d.mkdir()

        result = check_cache(tmp_path, SHA)

        assert result == "corrupt"


class TestAcquireLock:
    """Requirement: Lock file acquisition with exclusive creation."""

    def test_lock_acquired_when_absent(self, tmp_path: Path) -> None:
        lock_path = acquire_lock(tmp_path, SHA)

        assert lock_path == tmp_path / f"{SHA}.lock"
        assert lock_path.exists()

    def test_lock_busy_error_on_contention(self, tmp_path: Path) -> None:
        # Pre-create the lock file to simulate another process holding it
        (tmp_path / f"{SHA}.lock").touch()

        with pytest.raises(LockBusyError) as exc_info:
            acquire_lock(tmp_path, SHA)

        assert exc_info.value.lock_path == tmp_path / f"{SHA}.lock"

    def test_lock_busy_error_contains_path(self, tmp_path: Path) -> None:
        (tmp_path / f"{SHA}.lock").touch()

        with pytest.raises(LockBusyError) as exc_info:
            acquire_lock(tmp_path, SHA)

        assert str(tmp_path / f"{SHA}.lock") in str(exc_info.value)


class TestReleaseLock:
    """Requirement: Lock released on success and on exception."""

    def test_removes_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / f"{SHA}.lock"
        lock_path.touch()

        release_lock(lock_path)

        assert not lock_path.exists()

    def test_no_error_when_lock_already_absent(self, tmp_path: Path) -> None:
        lock_path = tmp_path / f"{SHA}.lock"
        # Should not raise even if the file doesn't exist
        release_lock(lock_path)

    def test_lock_released_on_exception_via_finally(self, tmp_path: Path) -> None:
        lock_path = acquire_lock(tmp_path, SHA)

        try:
            raise RuntimeError("simulated clone failure")
        except RuntimeError:
            pass
        finally:
            release_lock(lock_path)

        assert not lock_path.exists()


class TestWriteMetadata:
    """Requirement: Metadata file written after successful materialisation."""

    def test_writes_metadata_json(self, tmp_path: Path) -> None:
        write_metadata(tmp_path, SHA, requested_ref="main", repo_url="git@github.com:org/repo.git")

        meta_path = tmp_path / f"{SHA}-meta.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["requested_ref"] == "main"
        assert data["resolved_sha"] == SHA
        assert data["repo"] == "git@github.com:org/repo.git"
        assert "resolved_at" in data

    def test_does_not_overwrite_existing_metadata(self, tmp_path: Path) -> None:
        meta_path = tmp_path / f"{SHA}-meta.json"
        original = {"note": "original", "resolved_sha": SHA}
        meta_path.write_text(json.dumps(original))

        write_metadata(tmp_path, SHA, requested_ref="feature", repo_url="http://example.com")

        data = json.loads(meta_path.read_text())
        # Original content must be preserved
        assert data["note"] == "original"
        assert "feature" not in str(data)

    def test_resolved_at_is_iso8601_utc(self, tmp_path: Path) -> None:
        write_metadata(tmp_path, SHA, requested_ref="main", repo_url="url")

        meta_path = tmp_path / f"{SHA}-meta.json"
        data = json.loads(meta_path.read_text())
        # ISO 8601 UTC timestamps end with +00:00
        assert data["resolved_at"].endswith("+00:00")


class TestEnsureCacheRoot:
    """Requirement: Cache root created with mode 0700."""

    def test_creates_directory(self, tmp_path: Path) -> None:
        cache_root = tmp_path / "workspaces"
        ensure_cache_root(cache_root)

        assert cache_root.is_dir()

    def test_creates_parents(self, tmp_path: Path) -> None:
        cache_root = tmp_path / "deep" / "nested" / "workspaces"
        ensure_cache_root(cache_root)

        assert cache_root.is_dir()

    def test_idempotent_when_already_exists(self, tmp_path: Path) -> None:
        cache_root = tmp_path / "workspaces"
        cache_root.mkdir()

        # Should not raise
        ensure_cache_root(cache_root)
        assert cache_root.is_dir()
