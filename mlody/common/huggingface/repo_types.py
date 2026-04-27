"""Repository type primitives for the Hugging Face downloader."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class RepoType(str, Enum):
    """Supported Hugging Face repository categories."""

    MODEL = "model"
    DATASET = "dataset"

    @classmethod
    def from_dataset_flag(cls, is_dataset: bool) -> RepoType:
        """Return the repo type implied by the ``--dataset`` flag."""
        return cls.DATASET if is_dataset else cls.MODEL

    @property
    def info_kind(self) -> str:
        """Return the human-readable info kind used in CLI output."""
        return "dataset" if self is RepoType.DATASET else "model"

    @property
    def metadata_filename(self) -> str:
        """Return the metadata filename stored alongside a downloaded snapshot."""
        return f"{self.info_kind}_info.json"

    def default_base_out(self, repo_id: str) -> Path:
        """Return the default cache directory for a repo before SHA suffixing."""
        vendor, name = repo_id.split("/", maxsplit=1)
        base = Path("~/.cache/mlody/artifacts/huggingface").expanduser()
        if self is RepoType.DATASET:
            base /= "datasets"
        return base / vendor / name


def coerce_repo_type(value: RepoType | str) -> RepoType:
    """Normalize compatibility string inputs to ``RepoType``."""
    if isinstance(value, RepoType):
        return value
    return RepoType(value)


REPO_TYPE_MODEL = RepoType.MODEL
REPO_TYPE_DATASET = RepoType.DATASET
