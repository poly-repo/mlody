"""Transport and download planning for Hugging Face snapshots."""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from mlody.common.huggingface.repo_client import build_file_url
from mlody.common.huggingface.repo_types import (
    REPO_TYPE_MODEL,
    RepoType,
    coerce_repo_type,
)
from mlody.common.huggingface.resume_state import (
    initialize_partial_state,
    load_partial_metadata,
    mark_segment_completed,
    partial_metadata_path,
    partial_path,
)

SEGMENT_SIZE = 64 * 1024 * 1024
LARGE_FILE_THRESHOLD = 200 * 1024 * 1024
REQUEST_TIMEOUT = (10, 60)


@dataclass(frozen=True)
class Segment:
    """One byte range in a segmented download plan."""

    index: int
    start: int
    end: int


def measure_bandwidth() -> float:
    """Estimate downstream bandwidth in Gbps using a small Hub file."""
    test_url = "https://huggingface.co/gpt2/resolve/main/config.json"

    start = time.time()
    with requests.get(test_url, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        size = len(response.content)
    elapsed = time.time() - start

    bps = size / elapsed
    gbps = bps * 8 / 1e9
    return max(gbps, 0.1)


def estimate_workers(gbps: float) -> int:
    """Estimate an I/O-heavy worker count from measured bandwidth."""
    cores = multiprocessing.cpu_count()
    cpu_limit = cores * 4
    net_limit = int(gbps * 16)
    return max(4, min(cpu_limit, net_limit))


def plan_segments(
    size: int,
    *,
    segment_size: int | None = None,
) -> tuple[Segment, ...]:
    """Return the typed download plan for a file of ``size`` bytes."""
    resolved_segment_size = SEGMENT_SIZE if segment_size is None else segment_size
    segments: list[Segment] = []
    for index, start in enumerate(range(0, size, resolved_segment_size)):
        end = min(start + resolved_segment_size - 1, size - 1)
        segments.append(Segment(index=index, start=start, end=end))
    return tuple(segments)


def build_segments(
    size: int,
    *,
    segment_size: int | None = None,
) -> list[tuple[int, int]]:
    """Compatibility helper returning raw segment ranges."""
    return [
        (segment.start, segment.end)
        for segment in plan_segments(size, segment_size=segment_size)
    ]


def download_segment(
    url: str,
    start: int,
    end: int,
    path: Path,
    token: str | None,
) -> None:
    """Download one byte range into an existing partial file."""
    headers = {"Range": f"bytes={start}-{end}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    expected_size = end - start + 1
    written = 0

    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        response.raise_for_status()
        if response.status_code != 206:
            raise RuntimeError(
                f"Range request for {path} returned {response.status_code} instead of 206"
            )

        with path.open("r+b") as handle:
            handle.seek(start)
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)

    if written != expected_size:
        raise RuntimeError(
            f"Segment {start}-{end} for {path} wrote {written} bytes, expected {expected_size}"
        )


def segmented_download(
    url: str,
    dest: Path,
    token: str | None,
    workers: int,
) -> None:
    """Download a large file via resumable range requests."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.head(
        url,
        headers=headers,
        allow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    size = int(response.headers["Content-Length"])

    segments = plan_segments(size)
    metadata = load_partial_metadata(
        dest,
        size,
        len(segments),
        segment_size=SEGMENT_SIZE,
    )
    if metadata is None:
        metadata = initialize_partial_state(
            dest,
            size,
            len(segments),
            segment_size=SEGMENT_SIZE,
        )

    pending_segments = [
        segment
        for segment in segments
        if not metadata.completed_segments[segment.index]
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                download_segment,
                url,
                segment.start,
                segment.end,
                dest,
                token,
            ): segment.index
            for segment in pending_segments
        }

        for future in concurrent.futures.as_completed(futures):
            future.result()
            metadata = mark_segment_completed(dest, metadata, futures[future])


def download_file(
    repo: str,
    revision: str,
    file_path: str,
    dest: Path,
    token: str | None,
    workers: int,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
) -> None:
    """Download one repo file into the target snapshot directory."""
    resolved_type = coerce_repo_type(repo_type)
    url = build_file_url(repo, revision, file_path, repo_type=resolved_type)

    output_path = dest / file_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.head(
        url,
        headers=headers,
        allow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    size = int(response.headers.get("Content-Length", 0))

    partial_output = partial_path(output_path)
    metadata_path = partial_metadata_path(partial_output)

    if output_path.exists() and output_path.stat().st_size == size:
        if partial_output.exists():
            partial_output.unlink()
        if metadata_path.exists():
            metadata_path.unlink()
        return

    if size > LARGE_FILE_THRESHOLD:
        segmented_download(url, partial_output, token, workers)
    else:
        with requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()
            with partial_output.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)

    os.replace(partial_output, output_path)
    if metadata_path.exists():
        metadata_path.unlink()


def download_repo(
    repo: str,
    revision: str,
    dest: Path,
    files: list[str] | tuple[str, ...],
    workers: int,
    token: str | None,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
    *,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Download all requested files in parallel."""
    resolved_type = coerce_repo_type(repo_type)
    dest.mkdir(parents=True, exist_ok=True)
    start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                download_file,
                repo,
                revision,
                file_path,
                dest,
                token,
                workers,
                resolved_type,
            )
            for file_path in files
        ]

        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            future.result()
            print_fn(f"[{index}/{len(files)}] complete")

    elapsed = time.time() - start
    print_fn(f"\nFinished in {elapsed:.1f}s")
