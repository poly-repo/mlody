"""Public factory for workspace resolution — parse, resolve, materialise."""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import socket
from pathlib import Path
from typing import Callable, NamedTuple

from mlody.core.workspace import Workspace
from mlody.db.evaluations import open_db, write_evaluation
from mlody.db.local_diff import compute_local_diff_sha, get_repo_root
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
    NoMlodyAtCommitError,
    UnknownRefError,
)
from mlody.resolver.git_client import GitClient

_logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SUFFIX = Path(".cache") / "mlody" / "workspaces"
_DEFAULT_DB_SUFFIX = Path(".cache") / "mlody" / "mlody.sqlite"


def _get_username() -> str:
    """Return the OS username; falls back to pwd lookup if os.getlogin() raises."""
    try:
        return os.getlogin()
    except OSError:
        return pwd.getpwuid(os.getuid()).pw_name


def _record_evaluation_best_effort(
    *,
    resolved_sha: str,
    committoid: str,
    repo_url: str,
    local_only: bool,
    resolved_at: str,
    value_description: str,
) -> None:
    """Write one evaluation row to the local SQLite DB.

    Best-effort: logs at ERROR level and returns on any failure so a DB error
    never terminates the caller (NFR-AVAIL-001). Connection is always closed.
    """
    db_path = Path.home() / _DEFAULT_DB_SUFFIX
    conn = None
    try:
        conn = open_db(db_path)
        local_diff_sha = compute_local_diff_sha(get_repo_root())
        write_evaluation(
            conn,
            username=_get_username(),
            hostname=socket.gethostname(),
            requested_ref=committoid,
            resolved_sha=resolved_sha,
            resolved_at=resolved_at,
            repo=repo_url,
            local_only=local_only,
            value_description=value_description,
            local_diff_sha=local_diff_sha,
        )
    except Exception as exc:
        _logger.error("Failed to write evaluation to %s: %s", db_path, exc)
    finally:
        if conn is not None:
            conn.close()


class ResolvedRef(NamedTuple):
    """Result of SHA resolution — the full 40-char SHA and provenance flag."""

    sha: str
    local_only: bool


def parse_label(label: str) -> tuple[str | None, str]:
    """Split a raw label into (committoid, inner_label).

    Delegates to the core label parser and projects the resulting Label
    into the (committoid, inner_label) shape expected by resolver callers.
    Raises LabelParseError when the label has neither an entity spec nor an
    attribute path (i.e. it cannot resolve to any value).

    # TODO(mlody-label-parsing): replace callers with Label directly and delete wrapper.
    """
    from mlody.core.label import parse_label as _core_parse_label
    from mlody.core.label.errors import LabelParseError as _LabelParseError

    lbl = _core_parse_label(label)  # raises LabelParseError on bad input

    committoid = lbl.workspace  # None = CWD

    if lbl.entity is None and lbl.attribute_path is None:
        # Bare workspace label (e.g. "HEAD", "main", "abc123f").
        # inner_label is empty — callers resolve this to MlodyWorkspaceValue.
        return (committoid, "")

    if lbl.entity is None:
        # Workspace-level attribute access (e.g. "'info", "457f'info").
        # Re-serialise the attribute portion as inner_label for workspace.resolve.
        assert lbl.attribute_path is not None  # guaranteed by the check above
        attr_str = ".".join(lbl.attribute_path)
        if lbl.attribute_query:
            attr_str += f"[{lbl.attribute_query}]"
        return (committoid, f"'{attr_str}")

    # Entity-bearing label: re-serialise inner_label from Label fields.
    parts: list[str] = []
    if lbl.entity.root is not None:
        parts.append(f"@{lbl.entity.root}")
    path = lbl.entity.path or ""
    if lbl.entity.wildcard:
        parts.append(f"//{path}/...")
    elif path:
        parts.append(f"//{path}")
    # else: bare root (@lexica with no path) — no // suffix
    if lbl.entity.name is not None:
        parts.append(f":{lbl.entity.name}")
        if lbl.entity.field_path:
            parts.append("." + ".".join(lbl.entity.field_path))
        if lbl.entity_query is not None:
            parts.append(f"[{lbl.entity_query}]")
    if lbl.attribute_path:
        parts.append("'" + ".".join(lbl.attribute_path))
        if lbl.attribute_query is not None:
            parts.append(f"[{lbl.attribute_query}]")
    inner_label = "".join(parts)
    return (committoid, inner_label)


def resolve_sha(committoid: str, git_client: GitClient) -> ResolvedRef:
    """Resolve a committoid (branch, tag, short/full SHA) to a ResolvedRef.

    Resolution order:
    1. Exact branch match (refs/heads/<name>)
    2. Exact tag match (refs/tags/<name>), preferring the ^{} deref SHA for
       annotated tags over the tag object SHA.
    3. If both a branch and a tag match, raise BranchTagCollisionError.
    4. SHA prefix match across all remote SHAs — unique match returns the full
       SHA; multiple matches raise AmbiguousRefError.
    5. Local remote-tracking refs — covers merged/deleted branches fetched
       locally but no longer on the remote (local_only=False, was landed).
    6. Local-only fallback via git rev-parse — covers branches and SHAs that
       exist only in the CWD and have never been pushed (local_only=True).
    7. Nothing matched — raise UnknownRefError.
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
        return ResolvedRef(exact_shas.pop(), False)

    # SHA prefix match — search across all (sha, ref) pairs
    all_shas = {sha for sha, _ in pairs}
    prefix_matches = {sha for sha in all_shas if sha.startswith(committoid)}
    if len(prefix_matches) == 1:
        return ResolvedRef(prefix_matches.pop(), False)
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
        return ResolvedRef(local_branch_shas.pop(), False)

    # Local-only fallback — branch or SHA exists only in the CWD, not pushed.
    local_sha = git_client.rev_parse_local(committoid)
    if local_sha:
        _logger.debug(
            "Ref %r not found on remote; resolved from local repo (not landed)", committoid
        )
        return ResolvedRef(local_sha, True)

    raise UnknownRefError(committoid, "origin")


def materialise(
    full_sha: str,
    monorepo_root: Path,
    git_client: GitClient,
    cache_root: Path,
    committoid: str,
    local_only: bool = False,
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
        write_metadata(cache_root, full_sha, requested_ref=committoid, repo_url=repo_url, local_only=local_only)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        release_lock(lock_path)

    return dest


def _workspace_injections(
    monorepo_root: Path, workspace_root: Path
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Compute extra_roots and lazy_roots for --workspace mode.

    The evaluator always uses monorepo_root as its root_path so that
    //mlody/... load() paths continue to resolve from the top of the monorepo.
    Workspace-specific roots are expressed as monorepo-relative paths.

    When workspace_root ≠ monorepo_root (i.e. --workspace DIR was given):

    - extra_roots: {"workspace": <dir>} — points @workspace at workspace_root
      using a monorepo-relative path so Phase 2 eagerly loads sandbox .mlody
      files and @workspace//... labels work.

    - lazy_roots: {"mlody": "mlody"} — points @mlody at monorepo_root/mlody so
      sandboxes can load("@mlody//common/...") on demand without pre-globbing.

    Returns (None, None) when workspace_root == monorepo_root.
    """
    if workspace_root == monorepo_root:
        return None, None

    workspace_rel = str(workspace_root.relative_to(monorepo_root))
    extra_roots: dict[str, str] = {"workspace": workspace_rel}

    lazy_roots: dict[str, str] | None = None
    if (monorepo_root / "mlody").is_dir():
        lazy_roots = {"mlody": "mlody"}

    return extra_roots, lazy_roots


def resolve_workspace(
    label: str,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    roots_file: Path | None = None,
    full_workspace: bool = False,
    print_fn: Callable[..., None] = print,
    git_client: GitClient | None = None,
    cache_root: Path | None = None,
    verbose: bool = False,
    value_description: str | None = None,
) -> tuple[Workspace, str | None]:
    """Resolve a raw label to a ready Workspace and optional resolved SHA.

    For cwd-relative labels (@//-prefixed) the monorepo_root workspace is used
    directly and resolved_sha is None. When workspace_root is provided it is
    used instead of monorepo_root as the // anchor for CWD-relative labels —
    this corresponds to the --workspace CLI flag. For committoid-qualified
    labels the resolver fetches the remote SHA, materialises a cached clone,
    and returns a Workspace rooted there along with the full 40-char SHA.
    workspace_root has no effect on committoid-qualified labels.

    When value_description is provided (non-None, non-empty), a row is written
    to the local SQLite evaluations DB after successful materialisation. The
    write is best-effort and never raises (NFR-AVAIL-001).

    All error conditions raise WorkspaceResolutionError subclasses — callers
    are responsible for catching and formatting them.
    """
    committoid, inner_label = parse_label(label)

    if committoid is None:
        ws_root = workspace_root if workspace_root is not None else monorepo_root
        extra_roots, lazy_roots = _workspace_injections(monorepo_root, ws_root)
        ws = Workspace(
            monorepo_root=monorepo_root,
            roots_file=roots_file,
            full_workspace=full_workspace,
            print_fn=print_fn,
            extra_roots=extra_roots,
            lazy_roots=lazy_roots,
            workspace_root=ws_root if ws_root != monorepo_root else None,
        )
        ws.load(verbose=verbose)
        return (ws, None)

    client = git_client or GitClient(monorepo_root)
    root = cache_root or (Path.home() / _DEFAULT_CACHE_SUFFIX)
    ensure_cache_root(root)

    resolved = resolve_sha(committoid, client)
    _logger.debug("Resolved %s to %s", committoid, resolved.sha)

    dest = materialise(resolved.sha, monorepo_root, client, root, committoid, local_only=resolved.local_only)

    if value_description:
        from datetime import datetime, timezone

        _record_evaluation_best_effort(
            resolved_sha=resolved.sha,
            committoid=committoid,
            repo_url=client.remote_url() or "",
            local_only=resolved.local_only,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            value_description=value_description,
        )

    ws = Workspace(
        monorepo_root=dest,
        roots_file=None,
        full_workspace=full_workspace,
        print_fn=print_fn,
    )
    try:
        ws.load(verbose=verbose)
    except FileNotFoundError:
        raise NoMlodyAtCommitError(committoid, resolved.sha) from None
    return (ws, resolved.sha)
