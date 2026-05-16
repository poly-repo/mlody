"""Public factory for workspace resolution — parse, resolve, materialise."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import pwd
import shutil
import socket
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple

from mlody.core.workspace import Workspace, WorkspaceStateKind
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
    WorkspaceResolutionError,
)
from mlody.resolver.git_client import GitClient

_logger = logging.getLogger(__name__)
_WORKSPACE_TYPE = Workspace

_DEFAULT_CACHE_SUFFIX = Path(".cache") / "mlody" / "workspaces"
_DEFAULT_DB_SUFFIX = Path(".cache") / "mlody" / "mlody.sqlite"


@dataclass(frozen=True, unsafe_hash=True)
class WorkspaceRequest:
    """Immutable description of one baseline workspace build request.

    Frozen dataclass so it is usable as a dict key directly — no separate
    factory function needed.  print_fn and console compare by Python object
    identity (the default for callables and arbitrary objects), which gives
    the same cache-hit semantics as the former id()-based approach.
    """

    mode: str
    monorepo_root: Path
    workspace_root: Path
    roots_file: Path
    full_workspace: bool
    extra_roots: tuple[tuple[str, str], ...]
    lazy_roots: tuple[tuple[str, str], ...]
    print_fn: Callable[..., None]
    console: object | None
    resolved_sha: str | None

    def cache_key(self) -> WorkspaceRequest:
        """Return self — WorkspaceRequest is its own cache key."""
        return self


@dataclass
class Reporter:
    """Carries the output channels and verbosity flag for workspace operations."""

    print_fn: Callable[..., None]
    console: object | None = None
    verbose: bool = False


_BASELINE_WORKSPACE_CACHE: dict[WorkspaceRequest, Workspace] = {}


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


def _load_baseline_workspace(
    request: WorkspaceRequest,
    reporter: Reporter,
) -> Workspace:
    """Construct, load, and baseline-normalise a workspace."""
    workspace = Workspace(
        monorepo_root=request.monorepo_root,
        roots_file=request.roots_file,
        full_workspace=request.full_workspace,
        print_fn=request.print_fn,
        console=request.console,
        extra_roots=dict(request.extra_roots) if request.extra_roots else None,
        lazy_roots=dict(request.lazy_roots) if request.lazy_roots else None,
        workspace_root=request.workspace_root if request.workspace_root != request.monorepo_root else None,
    )
    workspace.load(reporter=reporter)
    return build_baseline_workspace(workspace)


def get_or_build_baseline_workspace(
    request: WorkspaceRequest,
    reporter: Reporter,
) -> Workspace:
    """Return a cached baseline workspace or build one on a miss."""
    if reporter.verbose:
        reporter.print_fn(f"[mlody] cache key: {request!r}")

    cached = _BASELINE_WORKSPACE_CACHE.get(request)
    if cached is not None:
        _logger.debug("Baseline workspace cache hit for %r", request.mode)
        if reporter.verbose:
            reporter.print_fn(f"[mlody] cache hit for {request.mode!r}")
        return cached

    _logger.debug("Baseline workspace cache miss for %r", request.mode)
    if reporter.verbose:
        reporter.print_fn("[mlody] cache miss — building workspace")
    baseline = _load_baseline_workspace(request, reporter)
    _BASELINE_WORKSPACE_CACHE[request] = baseline
    return baseline


def evict_baseline_workspace(key: WorkspaceRequest) -> bool:
    """Remove one cached baseline workspace by identity key."""
    return _BASELINE_WORKSPACE_CACHE.pop(key, None) is not None


def reload_baseline_workspace(
    request: WorkspaceRequest,
    reporter: Reporter,
) -> Workspace:
    """Force a rebuild for one baseline workspace identity."""
    evict_baseline_workspace(request)
    return get_or_build_baseline_workspace(request, reporter)


def evict_cwd_baseline_workspaces(*, monorepo_root: Path | None = None) -> int:
    """Remove cached cwd baselines, optionally scoped to one monorepo root."""
    removed = 0
    for key in list(_BASELINE_WORKSPACE_CACHE):
        if key.mode != "cwd":
            continue
        if monorepo_root is not None and key.monorepo_root != monorepo_root:
            continue
        _BASELINE_WORKSPACE_CACHE.pop(key, None)
        removed += 1
    return removed


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

    # Entity-bearing label: delegate to the canonical formatter so query-only
    # wildcard labels (e.g. //...:[@mlody ...]) survive intact.
    return (committoid, lbl.format_inner())


def _parse_config_assignment(raw: str) -> tuple[str, str]:
    """Parse one ``LABEL=VALUE`` override string."""
    bracket_depth = 0
    quote: str | None = None
    escaped = False

    separator = -1
    for index, ch in enumerate(raw):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue

        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "[":
            bracket_depth += 1
            continue
        if ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            continue
        if ch == "=" and bracket_depth == 0:
            separator = index
            break

    if separator == -1:
        msg = f"Invalid --with override {raw!r}; expected LABEL=VALUE."
        raise WorkspaceResolutionError(msg)

    ref = raw[:separator].strip()
    value = raw[separator + 1 :]
    if not ref:
        msg = f"Invalid --with override {raw!r}; expected LABEL=VALUE."
        raise WorkspaceResolutionError(msg)
    return (ref, value)


def _updated_location_payload(location: object, value: object) -> object:
    """Return a location with its canonical payload updated."""
    from mlody.common.struct import struct_like_updated  # noqa: PLC0415

    raw_attributes = getattr(location, "attributes", None)
    if hasattr(raw_attributes, "as_mapping"):
        attributes = dict(raw_attributes.as_mapping())
    elif isinstance(raw_attributes, dict):
        attributes = dict(raw_attributes)
    else:
        attributes = {}
    attributes.pop("data", None)
    return struct_like_updated(location, data=value, attributes=attributes)


def _coerce_config_value(type_ref: object, value: str, raw: str) -> object:
    """Parse *value* from string, validate against *type_ref*, return canonical form.

    Raises WorkspaceResolutionError when the value cannot be parsed or is invalid.
    """
    type_name = getattr(type_ref, "name", "?")
    canonical = getattr(type_ref, "canonical", None)
    if callable(canonical):
        try:
            value = canonical(value)
        except (TypeError, ValueError) as exc:
            raise WorkspaceResolutionError(
                f"--with {raw!r}: value {value!r} is not valid for type {type_name!r}: {exc}"
            ) from exc
    validator = getattr(type_ref, "validator", None)
    if callable(validator):
        try:
            validator(value)
        except (TypeError, ValueError) as exc:
            raise WorkspaceResolutionError(
                f"--with {raw!r}: value {value!r} is not valid for type {type_name!r}: {exc}"
            ) from exc
    return value


def _registry_value_label(
    workspace: Workspace,
    key: tuple[object, object, object],
) -> str | None:
    """Return a concrete label for a registry value key when one can be derived."""
    _, stem, name = key
    if not isinstance(stem, str) or not isinstance(name, str):
        return None

    root_infos = getattr(workspace, "root_infos", {})
    root_matches: list[tuple[int, str, str]] = []
    for root_name, root_info in root_infos.items():
        if not isinstance(root_name, str):
            continue
        root_path = getattr(root_info, "path", None)
        if not isinstance(root_path, str):
            continue
        root_prefix = root_path.lstrip("/").rstrip("/")
        if not root_prefix:
            continue
        if stem == root_prefix or stem.startswith(root_prefix + "/"):
            root_matches.append((len(root_prefix), root_name, root_prefix))
    if root_matches:
        _, root_name, root_prefix = max(root_matches, key=lambda item: item[0])
        suffix = stem[len(root_prefix) :].lstrip("/")
        if suffix:
            return f"@{root_name}//{suffix}:{name}"
        return f"@{root_name}//:{name}"

    workspace_root = getattr(workspace, "_workspace_root", None)
    monorepo_root = getattr(workspace, "_monorepo_root", None)
    workspace_prefix = ""
    if isinstance(workspace_root, Path) and isinstance(monorepo_root, Path):
        if workspace_root != monorepo_root:
            try:
                workspace_prefix = str(workspace_root.relative_to(monorepo_root))
            except ValueError:
                return None
            workspace_prefix = workspace_prefix.strip("/")
    if workspace_prefix:
        if stem == workspace_prefix:
            relative_stem = ""
        elif stem.startswith(workspace_prefix + "/"):
            relative_stem = stem[len(workspace_prefix) + 1 :]
        else:
            return None
    else:
        relative_stem = stem
    if relative_stem:
        return f"//{relative_stem}:{name}"
    return f"//:{name}"


def _normalize_workspace_defaults(workspace: Workspace) -> Workspace:
    """Populate missing value payloads from defaults before user overrides apply."""
    from mlody.common.struct import struct_like_as_mapping, struct_like_updated  # noqa: PLC0415
    from mlody.core.lineage import append_lineage, build_lineage_event  # noqa: PLC0415
    from mlody.core.setf import setf  # noqa: PLC0415

    for key, value in workspace.registry_view.iter_registry_items():
        if not (
            isinstance(key, tuple)
            and len(key) == 3
            and key[0] == "value"
        ):
            continue
        default_value = getattr(value, "default", None)
        if default_value is None:
            continue
        location = getattr(value, "location", None)
        if location is None:
            continue
        try:
            location_fields = struct_like_as_mapping(location)
        except TypeError:
            location_fields = {}
        if location_fields.get("data", None) is not None:
            continue

        updated_location = _updated_location_payload(location, default_value)
        unit_ref = getattr(value, "unit", None)
        if unit_ref is not None:
            from common.python.starlarkish.evaluator.evaluator import (  # noqa: PLC0415
                _format_quantity_string,
            )
            source = f"DEFAULT: {_format_quantity_string(default_value, unit_ref)}"
        else:
            source = f"DEFAULT: {default_value}"
        label = _registry_value_label(workspace, key)
        if label is not None:
            setf(
                f"{label}.location",
                updated_location,
                workspace=workspace,
                source=source,
            )
            continue

        updated_value = struct_like_updated(value, location=updated_location)
        event = build_lineage_event(
            accessor=str(key),
            new_value=updated_location,
            source=source,
            reason=None,
            timestamp=None,
            mode="inplace",
        )
        updated_value = append_lineage(updated_value, event, mode="inplace")
        workspace.registry_view.set_registry_entity(key, updated_value)
    return workspace


def _canonicalize_config_label(label: str, registry_key: str) -> str:
    """Expand a relative config-rules label to an absolute //stem:name form.

    Relative forms supported:
      :name        → entity in the same file as the config
      path:name    → entity in path relative to the config's directory
      //...        → returned unchanged (already absolute)
      @...         → returned unchanged (already absolute)
    """
    if label.startswith("//") or label.startswith("@"):
        return label
    config_file_stem = registry_key.rsplit(":", 1)[0]
    config_dir = str(Path(config_file_stem).parent).replace("\\", "/")
    if config_dir == ".":
        config_dir = ""
    if label.startswith(":"):
        return f"//{config_file_stem}:{label[1:]}"
    if ":" in label:
        rel_path, name = label.split(":", 1)
        stem = f"{config_dir}/{rel_path}" if config_dir else rel_path
        return f"//{stem}:{name}"
    return f"//{config_file_stem}:{label}"


def _apply_registered_configs(workspace: Workspace) -> None:
    """Apply all registered config rules after defaults have been normalised.

    Called from configure_workspace after _normalize_workspace_defaults so that
    the precedence chain is correct: DEFAULT < CONFIG < COMMAND_LINE.
    """
    from mlody.common.config import RegisteredConfig  # noqa: PLC0415
    from mlody.core.setf import setf  # noqa: PLC0415

    for registry_key, config_struct in workspace.registry_view.configs_snapshot():
        registered = RegisteredConfig(config_struct)
        for label, value in registered.rules.items():
            label = _canonicalize_config_label(label, registry_key)
            source = f"CONFIG: {registered.name}: {label}={value}"
            try:
                resolved = workspace.resolve(label)
            except Exception:
                resolved = None
            if getattr(resolved, "kind", None) == "value":
                location = getattr(resolved, "location", None)
                if getattr(location, "type", None) == "inline":
                    setf(
                        f"{label}.location",
                        _updated_location_payload(location, value),
                        workspace=workspace,
                        source=source,
                    )
                    continue
            setf(label, value, workspace=workspace, source=source)


def build_baseline_workspace(workspace: Workspace) -> Workspace:
    """Apply defaults and registered config rules exactly once on a loaded workspace."""
    if not isinstance(workspace, _WORKSPACE_TYPE):
        _normalize_workspace_defaults(workspace)
        _apply_registered_configs(workspace)
        return workspace

    if workspace.state_kind is WorkspaceStateKind.LOADED:
        _normalize_workspace_defaults(workspace)
        _apply_registered_configs(workspace)
        workspace.mark_baseline()
    return workspace


def apply_request_overrides(workspace: Workspace, config: Iterable[str]) -> Workspace:
    """Apply ``--with LABEL=VALUE`` overrides to a request-local workspace."""
    from mlody.core.setf import setf  # noqa: PLC0415

    for raw in config:
        ref, value = _parse_config_assignment(raw)
        concrete_refs = workspace.expand_wildcard_label(ref)
        if not concrete_refs:
            msg = f"--with override {raw!r} matched no entities."
            raise WorkspaceResolutionError(msg)
        for concrete_ref in concrete_refs:
            source = f"COMMAND_LINE: {raw}"
            try:
                resolved = workspace.resolve(concrete_ref)
            except AttributeError:
                resolved = None
            if getattr(resolved, "kind", None) == "value":
                type_ref = getattr(resolved, "type", None)
                unit_ref = getattr(resolved, "unit", None)
                if unit_ref is not None and isinstance(value, str):
                    from common.python.starlarkish.evaluator.evaluator import (  # noqa: PLC0415
                        _parse_quantity_string,
                    )
                    try:
                        value = _parse_quantity_string(value, unit_ref)
                    except ValueError as exc:
                        raise WorkspaceResolutionError(
                            f"--with {raw!r}: cannot parse as quantity for "
                            f"unit {unit_ref}: {exc}"
                        ) from exc
                if type_ref is not None:
                    value = _coerce_config_value(type_ref, value, raw)
                    if unit_ref is not None:
                        from common.python.starlarkish.evaluator.evaluator import (  # noqa: PLC0415
                            _format_quantity_string,
                        )
                        source = f"COMMAND_LINE: {ref}={_format_quantity_string(value, unit_ref)}"
                    else:
                        source = f"COMMAND_LINE: {ref}={value}"
                location = getattr(resolved, "location", None)
                if getattr(location, "type", None) == "inline":
                    setf(
                        f"{concrete_ref}.location",
                        _updated_location_payload(location, value),
                        workspace=workspace,
                        source=source,
                    )
                    continue
            setf(concrete_ref, value, workspace=workspace, source=source)
    return workspace


def configure_workspace(workspace: Workspace, config: Iterable[str]) -> Workspace:
    """Return a request-configured workspace without mutating a baseline in place."""
    if not isinstance(workspace, _WORKSPACE_TYPE):
        build_baseline_workspace(workspace)
        return apply_request_overrides(workspace, config)

    if workspace.state_kind is WorkspaceStateKind.REQUEST:
        return apply_request_overrides(workspace, config)

    baseline = build_baseline_workspace(workspace)
    request_workspace = baseline.fork_request()
    return apply_request_overrides(request_workspace, config)


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
    local_branch_shas = {
        sha for sha, ref in local_pairs if ref == f"refs/heads/{committoid}"
    }
    if len(local_branch_shas) == 1:
        _logger.debug(
            "Ref %r not found on remote; resolved from local remote-tracking ref",
            committoid,
        )
        return ResolvedRef(local_branch_shas.pop(), False)

    # Local-only fallback — branch or SHA exists only in the CWD, not pushed.
    local_sha = git_client.rev_parse_local(committoid)
    if local_sha:
        _logger.debug(
            "Ref %r not found on remote; resolved from local repo (not landed)",
            committoid,
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
        write_metadata(
            cache_root,
            full_sha,
            requested_ref=committoid,
            repo_url=repo_url,
            local_only=local_only,
        )
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


def _registered_users(workspace: Workspace) -> list[tuple[str, str]]:
    by_name = workspace.evaluator.registry.users.by_name
    if not isinstance(by_name, Mapping):
        return []

    users: list[tuple[str, str]] = []
    for raw_user in by_name.values():
        name = getattr(raw_user, "name", None)
        description = getattr(raw_user, "description", "")
        if not isinstance(name, str):
            continue
        users.append((name, description if isinstance(description, str) else ""))
    return sorted(users, key=lambda item: item[0])


def _format_valid_users(users: list[tuple[str, str]]) -> str:
    formatted: list[str] = []
    for name, description in users:
        if description and description != name:
            formatted.append(f"{name} ({description})")
        else:
            formatted.append(name)
    return ", ".join(formatted)


def _validate_workspace_user(workspace: Workspace, requested_user: str) -> str:
    users = _registered_users(workspace)
    if not users:
        msg = (
            f"User {requested_user!r} is invalid because this workspace has no "
            "registered users."
        )
        raise WorkspaceResolutionError(msg)

    matches = {
        name
        for name, description in users
        if requested_user == name or requested_user == description
    }
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        msg = (
            f"User {requested_user!r} is ambiguous; it matches multiple registered "
            f"users. Valid users: {_format_valid_users(users)}"
        )
        raise WorkspaceResolutionError(msg)

    msg = (
        f"User {requested_user!r} is not one of the valid registered users. "
        f"Valid users: {_format_valid_users(users)}"
    )
    raise WorkspaceResolutionError(msg)


def apply_workspace_user(
    workspace: Workspace,
    requested_user: str,
    *,
    resolved_sha: str | None,
) -> tuple[Workspace, str]:
    """Validate and apply a workspace user to request-local runtime context."""
    selected_user = _validate_workspace_user(workspace, requested_user)
    request_workspace = (
        workspace.fork_request() if isinstance(workspace, _WORKSPACE_TYPE) else workspace
    )
    update_global_context = getattr(request_workspace, "update_global_context", None)
    if callable(update_global_context):
        update_global_context(
            user=selected_user,
            resolved_sha=resolved_sha,
        )
    return (request_workspace, selected_user)


def _make_workspace_request(
    *,
    mode: str,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    roots_file: Path | None = None,
    full_workspace: bool = False,
    print_fn: Callable[..., None] = print,
    console: object | None = None,
    extra_roots: dict[str, str] | None = None,
    lazy_roots: dict[str, str] | None = None,
    resolved_sha: str | None = None,
) -> WorkspaceRequest:
    """Build a WorkspaceRequest from individual kwargs, applying defaults."""
    effective_workspace_root = workspace_root if workspace_root is not None else monorepo_root
    effective_roots_file = (
        roots_file if roots_file is not None else (monorepo_root / "mlody" / "roots.mlody")
    )
    return WorkspaceRequest(
        mode=mode,
        monorepo_root=monorepo_root,
        workspace_root=effective_workspace_root,
        roots_file=effective_roots_file,
        full_workspace=full_workspace,
        extra_roots=tuple(sorted((extra_roots or {}).items())),
        lazy_roots=tuple(sorted((lazy_roots or {}).items())),
        print_fn=print_fn,
        console=console,
        resolved_sha=resolved_sha,
    )


def resolve_workspace_baseline(
    label: str,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    roots_file: Path | None = None,
    full_workspace: bool = False,
    print_fn: Callable[..., None] = print,
    git_client: GitClient | None = None,
    cache_root: Path | None = None,
    verbose: bool = False,
) -> tuple[Workspace, str | None]:
    """Resolve a raw label to a loaded baseline workspace and optional SHA."""
    committoid, _inner_label = parse_label(label)
    reporter = Reporter(print_fn=print_fn, verbose=verbose)

    if committoid is None:
        ws_root = workspace_root if workspace_root is not None else monorepo_root
        extra_roots, lazy_roots = _workspace_injections(monorepo_root, ws_root)
        request = _make_workspace_request(
            mode="cwd",
            monorepo_root=monorepo_root,
            workspace_root=ws_root,
            roots_file=roots_file,
            full_workspace=full_workspace,
            print_fn=print_fn,
            extra_roots=extra_roots,
            lazy_roots=lazy_roots,
        )
        baseline = get_or_build_baseline_workspace(request, reporter)
        return (baseline, None)

    client = git_client or GitClient(monorepo_root)
    root = cache_root or (Path.home() / _DEFAULT_CACHE_SUFFIX)
    ensure_cache_root(root)

    resolved = resolve_sha(committoid, client)
    _logger.debug("Resolved %s to %s", committoid, resolved.sha)

    dest = materialise(
        resolved.sha,
        monorepo_root,
        client,
        root,
        committoid,
        local_only=resolved.local_only,
    )

    try:
        request = _make_workspace_request(
            mode="commit",
            monorepo_root=dest,
            workspace_root=dest,
            roots_file=None,
            full_workspace=full_workspace,
            print_fn=print_fn,
            resolved_sha=resolved.sha,
        )
        baseline = get_or_build_baseline_workspace(request, reporter)
        return (baseline, resolved.sha)
    except FileNotFoundError:
        raise NoMlodyAtCommitError(committoid, resolved.sha) from None


def resolve_workspace(
    label: str,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    config: list[str] = [],
    user: str | None = None,
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

    When user is provided, it is validated against registered users after the
    baseline workspace loads and before any configuration is applied. The
    validated canonical short name is stored in the global runtime context.

    When value_description is provided (non-None, non-empty), a row is written
    to the local SQLite evaluations DB after successful materialisation. The
    write is best-effort and never raises (NFR-AVAIL-001).

    All error conditions raise WorkspaceResolutionError subclasses — callers
    are responsible for catching and formatting them.
    """
    committoid, _inner_label = parse_label(label)

    baseline, resolved_sha = resolve_workspace_baseline(
        label,
        monorepo_root=monorepo_root,
        workspace_root=workspace_root,
        roots_file=roots_file,
        full_workspace=full_workspace,
        print_fn=print_fn,
        git_client=git_client,
        cache_root=cache_root,
        verbose=verbose,
    )

    workspace = baseline
    if user is not None:
        workspace, _selected_user = apply_workspace_user(
            baseline,
            user,
            resolved_sha=resolved_sha,
        )

    if value_description and committoid is not None and resolved_sha is not None:
        from datetime import datetime, timezone

        client = git_client or GitClient(monorepo_root)
        resolved = resolve_sha(committoid, client)
        _record_evaluation_best_effort(
            resolved_sha=resolved_sha,
            committoid=committoid,
            repo_url=client.remote_url() or "",
            local_only=resolved.local_only,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            value_description=value_description,
        )

    return (configure_workspace(workspace, config), resolved_sha)
