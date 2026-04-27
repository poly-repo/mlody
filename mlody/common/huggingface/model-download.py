#!/usr/bin/env python3

"""Compatibility shim for the mlody Hugging Face downloader."""

from mlody.common.huggingface.cli import build_parser as build_parser
from mlody.common.huggingface.cli import main as main
from mlody.common.huggingface.cli import normalize_argv as normalize_argv
from mlody.common.huggingface.download import (
    LARGE_FILE_THRESHOLD as LARGE_FILE_THRESHOLD,
)
from mlody.common.huggingface.download import REQUEST_TIMEOUT as REQUEST_TIMEOUT
from mlody.common.huggingface.download import SEGMENT_SIZE as SEGMENT_SIZE
from mlody.common.huggingface.download import build_segments as build_segments
from mlody.common.huggingface.download import download_file as download_file
from mlody.common.huggingface.download import download_repo as download_repo
from mlody.common.huggingface.download import download_segment as download_segment
from mlody.common.huggingface.download import estimate_workers as estimate_workers
from mlody.common.huggingface.download import measure_bandwidth as measure_bandwidth
from mlody.common.huggingface.download import segmented_download as segmented_download
from mlody.common.huggingface.repo_client import build_file_url as build_file_url
from mlody.common.huggingface.repo_client import fetch_repo_snapshot as fetch_repo_snapshot
from mlody.common.huggingface.repo_client import list_refs as list_refs
from mlody.common.huggingface.repo_client import list_tags as list_tags
from mlody.common.huggingface.repo_types import (
    REPO_TYPE_DATASET as REPO_TYPE_DATASET,
)
from mlody.common.huggingface.repo_types import REPO_TYPE_MODEL as REPO_TYPE_MODEL
from mlody.common.huggingface.repo_types import RepoType as RepoType
from mlody.common.huggingface.resume_state import (
    initialize_partial_state as initialize_partial_state,
)
from mlody.common.huggingface.resume_state import (
    load_partial_metadata as load_partial_metadata,
)
from mlody.common.huggingface.resume_state import (
    partial_metadata_path as partial_metadata_path,
)
from mlody.common.huggingface.resume_state import partial_path as partial_path


if __name__ == "__main__":
    main()
