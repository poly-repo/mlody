"""Tests for per-process remote staging."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from mlody.core.tabular import stage_remote_file
from mlody.core.tabular.remote_staging import RemoteFetchError, RemoteStagingManager


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    """Serve *tmp_path* over HTTP and return ``(base_url, root)``."""
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", tmp_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_stage_remote_file_downloads_once_per_uri(http_server: tuple[str, Path]) -> None:
    base_url, root = http_server
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")

    manager = RemoteStagingManager()
    uri = f"{base_url}/employees.csv"
    staged_one = manager.stage(uri)
    staged_two = manager.stage(uri)

    assert staged_one.path == staged_two.path
    assert staged_one.content_hash == staged_two.content_hash
    assert staged_one.path.exists()
    assert staged_one.path.suffix == ".csv"


def test_stage_remote_file_content_hash_is_stable(http_server: tuple[str, Path]) -> None:
    base_url, root = http_server
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")

    staged_one = stage_remote_file(f"{base_url}/employees.csv")
    staged_two = stage_remote_file(f"{base_url}/employees.csv")

    assert staged_one.content_hash == staged_two.content_hash
    assert staged_one.path.exists()


def test_stage_remote_file_preserves_remote_suffix(http_server: tuple[str, Path]) -> None:
    base_url, root = http_server
    source_path = root / "employees.parquet"
    source_path.write_text("parquet-bytes")

    staged = stage_remote_file(f"{base_url}/employees.parquet")

    assert staged.path.suffix == ".parquet"


def test_stage_remote_file_rejects_unsupported_scheme() -> None:
    with pytest.raises(RemoteFetchError, match="http/https"):
        stage_remote_file("file:///tmp/data.csv")


def test_stage_remote_file_logs_uri_access(
    http_server: tuple[str, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    base_url, root = http_server
    source_path = root / "employees.csv"
    source_path.write_text("name,age\nAlice,30\nBob,40\n")

    manager = RemoteStagingManager()
    uri = f"{base_url}/employees.csv"

    with caplog.at_level("INFO", logger="mlody.core.tabular.remote_staging"):
        manager.stage(uri)

    assert any(
        "Fetching remote URI" in record.message and uri in record.message
        for record in caplog.records
    )
    assert any(
        "Staged remote URI" in record.message and uri in record.message
        for record in caplog.records
    )
