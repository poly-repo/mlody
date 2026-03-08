"""Public factory for workspace resolution — parse, resolve, materialise."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from mlody.core.workspace import Workspace
from mlody.resolver.cache import (
    acquire_lock,
    cache_dir,
    check_cache,
    ensure_cache_root,
    release_lock,
    write_metadata,
)
from mlody.resolver.errors import (
    AmbiguousRefError,
    BranchTagCollisionError,
    CorruptCacheError,
    LabelParseError,
    NoMlodyAtCommitError,
    UnknownRefError,
)
from mlody.resolver.git_client import GitClient

_logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SUFFIX = Path(".cache") / "mlody" / "workspaces"


def parse_label(label: str) -> tuple[str | None, str]:
    """Split a raw label into (committoid, inner_label).

    Labels starting with '@' or '//' pass through as cwd-relative (committoid
    is None). All other labels must contain a '|' separator; the left part is
    the committoid and the right part is the inner label (which must itself
    start with '@' or '//').
    """
    if label.startswith("@") or label.startswith("//"):
        return (None, label)

    parts = label.split("|", maxsplit=1)
    if len(parts) != 2:  # noqa: PLR2004
        raise LabelParseError(
            label,
            "missing '|' separator and label does not start with '@' or '//'",
        )

    committoid, inner_label = parts
    if not (inner_label.startswith("@") or inner_label.startswith("//")):
        raise LabelParseError(
            label,
            f"inner label {inner_label!r} must start with '@' or '//'",
        )

    return (committoid, inner_label)


def resolve_sha(committoid: str, git_client: GitClient) -> str:
    """Resolve a committoid (branch, tag, short/full SHA) to a 40-char SHA.

    Resolution order:
    1. Exact branch match (refs/heads/<name>)
    2. Exact tag match (refs/tags/<name>), preferring the ^{} deref SHA for
       annotated tags over the tag object SHA.
    3. If both a branch and a tag match, raise BranchTagCollisionError.
    4. SHA prefix match across all remote SHAs — unique match returns the full
       SHA; multiple matches raise AmbiguousRefError.
    5. Nothing matched — raise UnknownRefError.
    """
    pairs = git_client.ls_remote()

    branch_shas = {sha for sha, ref in pairs if ref == f"refs/heads/{committoid}"}

    # Prefer the dereferenced SHA (^{}) for annotated tags; fall back to the
    # tag object SHA for lightweight tags.
    deref_shas = {sha for sha, ref in pairs if ref == f"refs/tags/{committoid}^{{}}"}
    plain_tag_shas = {sha for sha, ref in pairs if ref == f"refs/tags/{committoid}"}
    tag_shas = deref_shas if deref_shas else plain_tag_shas

    if branch_shas and tag_shas:
        head_sha = next(iter(branch_shas))
        tag_sha = next(iter(tag_shas))
        raise BranchTagCollisionError(committoid, head_sha, tag_sha)

    exact_shas = branch_shas | tag_shas
    if len(exact_shas) == 1:
        return exact_shas.pop()

    # SHA prefix match — search across all (sha, ref) pairs
    all_shas = {sha for sha, _ in pairs}
    prefix_matches = {sha for sha in all_shas if sha.startswith(committoid)}
    if len(prefix_matches) == 1:
        return prefix_matches.pop()
    if len(prefix_matches) > 1:
        raise AmbiguousRefError(committoid, sorted(prefix_matches))

    # Fall back to local remote-tracking refs — covers merged/deleted branches
    # that were fetched locally but no longer appear on the remote.
    local_pairs = git_client.local_remote_tracking_refs()
    local_branch_shas = {sha for sha, ref in local_pairs if ref == f"refs/heads/{committoid}"}
    if len(local_branch_shas) == 1:
        _logger.debug(
            "Ref %r not found on remote; resolved from local remote-tracking ref", committoid
        )
        return local_branch_shas.pop()

    raise UnknownRefError(committoid, "origin")


def materialise(
    full_sha: str,
    monorepo_root: Path,
    git_client: GitClient,
    cache_root: Path,
    committoid: str,
) -> Path:
    """Ensure a workspace directory for full_sha exists in cache_root.

    Checks the cache first — returns immediately on a hit. On a miss, acquires
    an exclusive lock, clones (local or remote depending on local commit
    availability), writes metadata, and releases the lock in a finally block.

    Partial directories are cleaned up if the clone fails.
    """
    status = check_cache(cache_root, full_sha)
    if status == "hit":
        return cache_dir(cache_root, full_sha)
    if status == "corrupt":
        raise CorruptCacheError(cache_dir(cache_root, full_sha))

    lock_path = acquire_lock(cache_root, full_sha)
    dest = cache_dir(cache_root, full_sha)
    try:
        local = git_client.cat_file_type(full_sha) == "commit"
        if local:
            git_client.clone_local(dest=dest, sha=full_sha)
        else:
            git_client.clone_remote(dest=dest, sha=full_sha)

        repo_url = git_client.remote_url()
        write_metadata(cache_root, full_sha, requested_ref=committoid, repo_url=repo_url)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        release_lock(lock_path)

    return dest


def resolve_workspace(
    label: str,
    monorepo_root: Path,
    roots_file: Path | None = None,
    print_fn: Callable[..., None] = print,
    git_client: GitClient | None = None,
    cache_root: Path | None = None,
) -> tuple[Workspace, str | None]:
    """Resolve a raw label to a ready Workspace and optional resolved SHA.

    For cwd-relative labels (@//-prefixed) the monorepo_root workspace is used
    directly and resolved_sha is None. For committoid-qualified labels the
    resolver fetches the remote SHA, materialises a cached clone, and returns
    a Workspace rooted there along with the full 40-char SHA.

    All error conditions raise WorkspaceResolutionError subclasses — callers
    are responsible for catching and formatting them.
    """
    committoid, inner_label = parse_label(label)

    if committoid is None:
        ws = Workspace(
            monorepo_root=monorepo_root,
            roots_file=roots_file,
            print_fn=print_fn,
        )
        ws.load()
        return (ws, None)

    client = git_client or GitClient(monorepo_root)
    root = cache_root or (Path.home() / _DEFAULT_CACHE_SUFFIX)
    ensure_cache_root(root)

    full_sha = resolve_sha(committoid, client)
    _logger.debug("Resolved %s to %s", committoid, full_sha)

    dest = materialise(full_sha, monorepo_root, client, root, committoid)
    ws = Workspace(
        monorepo_root=dest,
        roots_file=None,
        print_fn=print_fn,
    )
    try:
        ws.load()
    except FileNotFoundError:
        raise NoMlodyAtCommitError(committoid, full_sha) from None
    return (ws, full_sha)
