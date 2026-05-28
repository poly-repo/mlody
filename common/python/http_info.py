"""Shared HTTP metadata helpers for remote asset introspection."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

_log = logging.getLogger(__name__)

HTTP_INFO_USER_AGENT = "mlody-http-info/1.0"


@dataclass(frozen=True, slots=True)
class _GitHubContentTarget:
    owner: str
    repo: str
    ref: str
    path: str
    original_url: str


def _coerce_http_length(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_http_update_time(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_http_digest(value: object, digest_type: str) -> tuple[str | None, str | None]:
    text = str(value).strip()
    if not text:
        return None, None
    if digest_type == "md5":
        try:
            return base64.b64decode(text, validate=True).hex(), "md5"
        except (binascii.Error, ValueError):
            return text, "content_md5"
    if digest_type == "etag":
        normalized = text.removeprefix("W/").strip('"')
        return normalized or text, "etag"
    return text, digest_type


def _extract_http_digest(headers: Any) -> tuple[str | None, str | None]:
    content_md5 = headers.get("Content-MD5")
    if content_md5:
        return _normalize_http_digest(content_md5, "md5")
    etag = headers.get("ETag")
    if etag:
        return _normalize_http_digest(etag, "etag")
    return None, None


def _http_headers_info(resolved_url: str, headers: Any) -> dict[str, object]:
    digest, digest_type = _extract_http_digest(headers)
    return {
        "url": resolved_url,
        "digest": digest,
        "digest_type": digest_type,
        "length": _coerce_http_length(headers.get("Content-Length")),
        "update_time": _normalize_http_update_time(headers.get("Last-Modified")),
    }


def _github_request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_INFO_USER_AGENT,
        },
    )


def _load_json(request: Request) -> object:
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_github_content_target(uri: str) -> _GitHubContentTarget | None:
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.split("/") if part]

    owner: str
    repo: str
    ref: str
    path: str
    if host == "raw.githubusercontent.com" and len(parts) >= 4:
        owner, repo, ref = parts[:3]
        path = "/".join(parts[3:])
    elif host == "github.com" and len(parts) >= 5 and parts[2] in {"blob", "raw"}:
        owner, repo, _mode, ref = parts[:4]
        path = "/".join(parts[4:])
    else:
        return None

    if not path:
        return None
    return _GitHubContentTarget(
        owner=owner,
        repo=repo,
        ref=ref,
        path=path,
        original_url=uri,
    )


def _github_contents_api_url(target: _GitHubContentTarget) -> str:
    encoded_path = quote(target.path, safe="/")
    query = urlencode({"ref": target.ref})
    return (
        f"https://api.github.com/repos/{target.owner}/{target.repo}/contents/"
        f"{encoded_path}?{query}"
    )


def _github_commits_api_url(target: _GitHubContentTarget) -> str:
    query = urlencode({"path": target.path, "sha": target.ref, "per_page": 1})
    return f"https://api.github.com/repos/{target.owner}/{target.repo}/commits?{query}"


def _extract_github_update_time(payload: object) -> str | None:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    commit = payload[0].get("commit")
    if not isinstance(commit, dict):
        return None
    committer = commit.get("committer")
    if isinstance(committer, dict) and committer.get("date") is not None:
        return _normalize_http_update_time(committer["date"])
    author = commit.get("author")
    if isinstance(author, dict) and author.get("date") is not None:
        return _normalize_http_update_time(author["date"])
    return None


def _github_http_info(target: _GitHubContentTarget) -> dict[str, object]:
    contents_payload = _load_json(_github_request(_github_contents_api_url(target)))
    if not isinstance(contents_payload, dict):
        raise TypeError("GitHub contents API returned a non-object payload")

    commits_payload = _load_json(_github_request(_github_commits_api_url(target)))
    digest = contents_payload.get("sha")
    if digest is not None and not isinstance(digest, str):
        digest = str(digest)

    return {
        "url": contents_payload.get("download_url") or target.original_url,
        "digest": digest,
        "digest_type": "git_blob_sha1" if digest else None,
        "length": _coerce_http_length(contents_payload.get("size")),
        "update_time": _extract_github_update_time(commits_payload),
    }


def _generic_http_info(uri: str) -> dict[str, object]:
    base_headers = {"User-Agent": HTTP_INFO_USER_AGENT}
    request = Request(uri, headers=base_headers, method="HEAD")
    try:
        with urlopen(request) as response:
            return _http_headers_info(response.geturl(), response.headers)
    except HTTPError as exc:
        if exc.code not in {405, 501}:
            raise

    with urlopen(Request(uri, headers=base_headers, method="GET")) as response:
        return _http_headers_info(response.geturl(), response.headers)


def fetch_http_info(uri: object) -> dict[str, object]:
    """Return stable metadata for an HTTP-accessible artifact."""
    if not isinstance(uri, str):
        raise TypeError(
            f"python.http_info() expects a URI string, got {type(uri).__name__}"
        )

    target = _parse_github_content_target(uri)
    if target is not None:
        try:
            return _github_http_info(target)
        except Exception:
            _log.debug(
                "GitHub metadata lookup failed for %s; falling back to generic HEAD",
                uri,
                exc_info=True,
            )

    return _generic_http_info(uri)


__all__ = ["HTTP_INFO_USER_AGENT", "fetch_http_info"]
