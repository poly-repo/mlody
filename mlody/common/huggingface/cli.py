"""CLI entrypoint for the Hugging Face downloader."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

from mlody.common.huggingface.download import (
    download_repo,
    estimate_workers,
    measure_bandwidth,
)
from mlody.common.huggingface.repo_client import (
    RepoSnapshot,
    fetch_repo_snapshot,
    list_refs,
    list_tags,
)
from mlody.common.huggingface.repo_types import RepoType

_KNOWN_COMMANDS = {"download", "tags", "releases", "refs", "-h", "--help"}


def build_parser() -> argparse.ArgumentParser:
    """Construct the downloader argument parser."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    def add_dataset_flag(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--dataset",
            action="store_true",
            help="Treat repo as a dataset repository (default: model repository).",
        )

    download_parser = subparsers.add_parser(
        "download",
        help="Download a model snapshot",
    )
    download_parser.add_argument("repo")
    download_parser.add_argument("-o", "--out", default=None)
    download_parser.add_argument("-w", "--workers", type=int)
    download_parser.add_argument(
        "-r",
        "--revision",
        default=None,
        help=(
            "Specific model revision to download (commit SHA, branch, or tag). "
            "Defaults to latest when omitted."
        ),
    )
    add_dataset_flag(download_parser)

    tags_parser = subparsers.add_parser(
        "tags",
        help="List available tags for a model repo",
    )
    tags_parser.add_argument("repo")
    add_dataset_flag(tags_parser)

    releases_parser = subparsers.add_parser(
        "releases",
        help="List available releases (tags) for a model repo",
    )
    releases_parser.add_argument("repo")
    add_dataset_flag(releases_parser)

    refs_parser = subparsers.add_parser(
        "refs",
        help="List available branches and tags for a model repo",
    )
    refs_parser.add_argument("repo")
    add_dataset_flag(refs_parser)

    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    """Preserve the legacy ``model-download.py <repo> ...`` invocation form."""
    if argv and argv[0] not in _KNOWN_COMMANDS:
        return ["download", *argv]
    return argv


def main(argv: list[str] | None = None) -> None:
    """Run the downloader CLI."""
    parser = build_parser()
    effective_argv = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(effective_argv)

    token = os.environ.get("HF_TOKEN")
    repo_type = RepoType.from_dataset_flag(getattr(args, "dataset", False))

    if args.command is None:
        parser.print_help()
        return

    if args.command in {"tags", "releases"}:
        if args.command == "releases":
            print("Hugging Face releases are represented as git tags.\n")
        list_tags(args.repo, token, repo_type=repo_type)
        return

    if args.command == "refs":
        list_refs(args.repo, token, repo_type=repo_type)
        return

    _run_download_command(args, token=token, repo_type=repo_type)


def _run_download_command(
    args: argparse.Namespace,
    *,
    token: str | None,
    repo_type: RepoType,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Execute the ``download`` subcommand."""
    repo = str(args.repo)
    requested_revision = args.revision
    base_out = Path(args.out) if args.out is not None else repo_type.default_base_out(repo)

    print_fn(f"base_out: {base_out}")
    print_fn(f"Fetching {repo_type.info_kind} info...")
    snapshot = fetch_repo_snapshot(
        repo,
        requested_revision,
        token,
        repo_type=repo_type,
    )

    print_fn(f"\n{snapshot.info_kind.capitalize()} info:")
    print_fn(str(snapshot.info))
    print_fn(f"\nResolved commit SHA: {snapshot.resolved_revision}")

    if requested_revision:
        print_fn(f"Requested revision: {requested_revision}")
    else:
        print_fn("Requested revision: latest (default)")

    repo_dir = base_out / snapshot.resolved_revision
    if repo_dir.exists():
        print_fn(f"\n{snapshot.info_kind.capitalize()} already downloaded at {repo_dir}")
        return

    files = sorted(snapshot.files, key=lambda path: path.endswith(".safetensors"), reverse=True)
    workers = _resolve_workers(args.workers, print_fn=print_fn)
    print_fn(f"Workers: {workers}")

    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_repo_metadata(repo_dir, snapshot)

    print_fn("\nDownloading files...")
    download_repo(
        repo,
        snapshot.resolved_revision,
        repo_dir,
        list(files),
        workers,
        token,
        repo_type=repo_type,
        print_fn=print_fn,
    )


def _resolve_workers(
    requested_workers: int | None,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Return the explicit or measured worker count."""
    if requested_workers:
        return requested_workers

    print_fn("\nMeasuring bandwidth...")
    gbps = measure_bandwidth()
    print_fn(f"Estimated {gbps:.2f} Gbps")
    return estimate_workers(gbps)


def _write_repo_metadata(repo_dir: Path, snapshot: RepoSnapshot) -> None:
    """Persist the raw repo metadata beside the downloaded snapshot."""
    with (repo_dir / snapshot.metadata_filename).open("w") as handle:
        json.dump(snapshot.info.__dict__, handle, indent=2, default=str)
