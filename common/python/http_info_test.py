from __future__ import annotations

import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from common.python import http_info as http_info_module


class _FakeUrlResponse:
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self._url = url
        self.headers = headers or {}
        self._body = body

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeUrlResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def test_fetch_http_info_generic_head_returns_digest_length_and_update_time() -> None:
    response = _FakeUrlResponse(
        url="https://example.com/data.csv",
        headers={
            "Content-Length": "17",
            "ETag": '"abc123"',
            "Last-Modified": "Mon, 11 May 2026 14:32:11 GMT",
        },
    )

    with patch.object(http_info_module, "urlopen", return_value=response) as mocked_urlopen:
        result = http_info_module.fetch_http_info("https://example.com/data.csv")

    request = mocked_urlopen.call_args.args[0]
    assert request.get_method() == "HEAD"
    assert request.full_url == "https://example.com/data.csv"
    assert result == {
        "url": "https://example.com/data.csv",
        "digest": "abc123",
        "digest_type": "etag",
        "length": 17,
        "update_time": "2026-05-11T14:32:11Z",
    }


def test_fetch_http_info_falls_back_to_get_when_head_not_supported() -> None:
    response = _FakeUrlResponse(
        url="https://example.com/data.csv",
        headers={
            "Content-Length": "8",
            "Last-Modified": "Tue, 12 May 2026 10:15:00 GMT",
        },
    )

    with patch.object(
        http_info_module,
        "urlopen",
        side_effect=[
            HTTPError(
                "https://example.com/data.csv",
                405,
                "Method Not Allowed",
                hdrs=None,
                fp=None,
            ),
            response,
        ],
    ) as mocked_urlopen:
        result = http_info_module.fetch_http_info("https://example.com/data.csv")

    first_request = mocked_urlopen.call_args_list[0].args[0]
    second_request = mocked_urlopen.call_args_list[1].args[0]
    assert first_request.get_method() == "HEAD"
    assert second_request.get_method() == "GET"
    assert result == {
        "url": "https://example.com/data.csv",
        "digest": None,
        "digest_type": None,
        "length": 8,
        "update_time": "2026-05-12T10:15:00Z",
    }


def test_fetch_http_info_github_url_uses_contents_and_commits_api() -> None:
    contents = _FakeUrlResponse(
        url=(
            "https://api.github.com/repos/apache/airflow/contents/"
            "airflow-core/docs/tutorial/pipeline_example.csv?ref=main"
        ),
        body=json.dumps(
            {
                "download_url": (
                    "https://raw.githubusercontent.com/apache/airflow/main/"
                    "airflow-core/docs/tutorial/pipeline_example.csv"
                ),
                "sha": "deadbeefcafebabe",
                "size": 321,
            }
        ).encode("utf-8"),
    )
    commits = _FakeUrlResponse(
        url=(
            "https://api.github.com/repos/apache/airflow/commits?"
            "path=airflow-core%2Fdocs%2Ftutorial%2Fpipeline_example.csv&sha=main&per_page=1"
        ),
        body=json.dumps(
            [
                {
                    "commit": {
                        "committer": {
                            "date": "2026-05-11T15:45:12Z",
                        }
                    }
                }
            ]
        ).encode("utf-8"),
    )

    with patch.object(
        http_info_module,
        "urlopen",
        side_effect=[contents, commits],
    ) as mocked_urlopen:
        result = http_info_module.fetch_http_info(
            "https://raw.githubusercontent.com/apache/airflow/main/"
            "airflow-core/docs/tutorial/pipeline_example.csv"
        )

    first_request = mocked_urlopen.call_args_list[0].args[0]
    second_request = mocked_urlopen.call_args_list[1].args[0]
    assert first_request.full_url == (
        "https://api.github.com/repos/apache/airflow/contents/"
        "airflow-core/docs/tutorial/pipeline_example.csv?ref=main"
    )
    assert second_request.full_url == (
        "https://api.github.com/repos/apache/airflow/commits?"
        "path=airflow-core%2Fdocs%2Ftutorial%2Fpipeline_example.csv&sha=main&per_page=1"
    )
    assert result == {
        "url": (
            "https://raw.githubusercontent.com/apache/airflow/main/"
            "airflow-core/docs/tutorial/pipeline_example.csv"
        ),
        "digest": "deadbeefcafebabe",
        "digest_type": "git_blob_sha1",
        "length": 321,
        "update_time": "2026-05-11T15:45:12Z",
    }


def test_fetch_http_info_github_failure_falls_back_to_generic_head() -> None:
    fallback = _FakeUrlResponse(
        url="https://raw.githubusercontent.com/apache/airflow/main/docs/data.csv",
        headers={
            "Content-Length": "99",
            "ETag": '"etag-99"',
        },
    )

    with patch.object(
        http_info_module,
        "urlopen",
        side_effect=[RuntimeError("boom"), fallback],
    ) as mocked_urlopen:
        result = http_info_module.fetch_http_info(
            "https://raw.githubusercontent.com/apache/airflow/main/docs/data.csv"
        )

    first_request = mocked_urlopen.call_args_list[0].args[0]
    second_request = mocked_urlopen.call_args_list[1].args[0]
    assert "api.github.com/repos/apache/airflow/contents/docs/data.csv?ref=main" in first_request.full_url
    assert second_request.get_method() == "HEAD"
    assert second_request.full_url == "https://raw.githubusercontent.com/apache/airflow/main/docs/data.csv"
    assert result == {
        "url": "https://raw.githubusercontent.com/apache/airflow/main/docs/data.csv",
        "digest": "etag-99",
        "digest_type": "etag",
        "length": 99,
        "update_time": None,
    }


def test_fetch_http_info_rejects_non_string_uri() -> None:
    with pytest.raises(TypeError, match="expects a URI string"):
        http_info_module.fetch_http_info(123)
