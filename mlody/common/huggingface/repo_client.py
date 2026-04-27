"""Hugging Face API and metadata lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from huggingface_hub import HfApi, dataset_info, hf_hub_url, model_info

from mlody.common.huggingface.repo_types import (
    REPO_TYPE_MODEL,
    RepoType,
    coerce_repo_type,
)


@dataclass(frozen=True)
class RepoSnapshot:
    """Resolved repository metadata returned by the Hub."""

    repo_id: str
    requested_revision: str | None
    repo_type: RepoType
    info: object

    @property
    def resolved_revision(self) -> str:
        """Return the resolved commit SHA for the snapshot."""
        return str(getattr(self.info, "sha"))

    @property
    def info_kind(self) -> str:
        """Return the user-facing kind name used in CLI output."""
        return self.repo_type.info_kind

    @property
    def metadata_filename(self) -> str:
        """Return the metadata filename used for this snapshot."""
        return self.repo_type.metadata_filename

    @property
    def files(self) -> tuple[str, ...]:
        """Return sibling filenames advertised by the Hub metadata."""
        siblings = getattr(self.info, "siblings", [])
        return tuple(
            str(sibling.rfilename)
            for sibling in siblings
            if getattr(sibling, "rfilename", None)
        )


class RepoClient:
    """Small wrapper around ``huggingface_hub`` for downloader workflows."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    def fetch_snapshot(
        self,
        repo_id: str,
        revision: str | None,
        repo_type: RepoType | str = REPO_TYPE_MODEL,
    ) -> RepoSnapshot:
        """Fetch metadata for a model or dataset repository."""
        resolved_type = coerce_repo_type(repo_type)
        if resolved_type is RepoType.DATASET:
            info = dataset_info(repo_id, revision=revision, token=self._token)
        else:
            info = model_info(repo_id, revision=revision, token=self._token)
        return RepoSnapshot(
            repo_id=repo_id,
            requested_revision=revision,
            repo_type=resolved_type,
            info=info,
        )

    def build_file_url(
        self,
        repo_id: str,
        revision: str,
        file_path: str,
        repo_type: RepoType | str = REPO_TYPE_MODEL,
    ) -> str:
        """Build the Hub download URL for one file."""
        resolved_type = coerce_repo_type(repo_type)
        return hf_hub_url(
            repo_id=repo_id,
            filename=file_path,
            repo_type=resolved_type.value,
            revision=revision,
        )

    def list_repo_refs(
        self,
        repo_id: str,
        repo_type: RepoType | str = REPO_TYPE_MODEL,
    ) -> object:
        """Return the raw refs payload from the Hub API."""
        resolved_type = coerce_repo_type(repo_type)
        api = HfApi(token=self._token)
        return api.list_repo_refs(repo_id=repo_id, repo_type=resolved_type.value)


def fetch_repo_snapshot(
    repo_id: str,
    revision: str | None,
    token: str | None,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
) -> RepoSnapshot:
    """Compatibility helper for one-shot snapshot lookup."""
    return RepoClient(token=token).fetch_snapshot(repo_id, revision, repo_type=repo_type)


def build_file_url(
    repo_id: str,
    revision: str,
    file_path: str,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
) -> str:
    """Compatibility helper for URL construction."""
    return RepoClient(token=None).build_file_url(
        repo_id,
        revision,
        file_path,
        repo_type=repo_type,
    )


def list_tags(
    repo_id: str,
    token: str | None,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
    *,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Print all tags for a repo."""
    refs = RepoClient(token=token).list_repo_refs(repo_id, repo_type=repo_type)
    tags = getattr(refs, "tags", None) or []

    if not tags:
        print_fn("No tags found.")
        return

    print_fn(f"Found {len(tags)} tag(s):")
    for tag in tags:
        target_commit = getattr(tag, "target_commit", None) or getattr(
            tag,
            "commit_id",
            "",
        )
        print_fn(f"{tag.name}\t{target_commit}")


def list_refs(
    repo_id: str,
    token: str | None,
    repo_type: RepoType | str = REPO_TYPE_MODEL,
    *,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Print branches and tags for a repo."""
    refs = RepoClient(token=token).list_repo_refs(repo_id, repo_type=repo_type)
    branches = getattr(refs, "branches", None) or []
    tags = getattr(refs, "tags", None) or []

    if not branches and not tags:
        print_fn("No branches or tags found.")
        return

    print_fn(f"Found {len(branches)} branch(es) and {len(tags)} tag(s).")

    if branches:
        print_fn("\nBranches:")
        for branch in branches:
            target_commit = getattr(branch, "target_commit", None) or getattr(
                branch,
                "commit_id",
                "",
            )
            print_fn(f"{branch.name}\t{target_commit}")

    if tags:
        print_fn("\nTags:")
        for tag in tags:
            target_commit = getattr(tag, "target_commit", None) or getattr(
                tag,
                "commit_id",
                "",
            )
            print_fn(f"{tag.name}\t{target_commit}")
