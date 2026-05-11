# This is python, not starlark

from pathlib import Path
import getpass
import os
import uuid

import git
from common.python.starlarkish.core.struct import Struct, struct


def get_git_info(monorepo_root: Path | str | None = None) -> dict[str, str]:
    """Return git branch/commit for the given workspace root.

    Uses the provided ``monorepo_root`` when present. This avoids resolving git
    state from Bazel runfiles CWDs, which are not repository roots.
    """
    try:
        if monorepo_root is not None:
            repo_path = Path(monorepo_root)
        else:
            workspace_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
            repo_path = (
                Path(workspace_dir) if workspace_dir is not None else Path.cwd()
            )

        repo = git.Repo(repo_path, search_parent_directories=True)
        latest_commit = repo.head.commit
        return {
            "branch": repo.git.rev_parse("--abbrev-ref", "HEAD"),
            "commit": latest_commit.hexsha,
        }
    except Exception:
        return {}


def _default_workspace_directory(
    monorepo_root: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Path:
    if workspace_root is not None:
        return Path(workspace_root)
    if monorepo_root is not None:
        return Path(monorepo_root)

    workspace_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace_dir is not None:
        return Path(workspace_dir)
    return Path.cwd()


def _struct_fields(value: object) -> dict[str, object]:
    if isinstance(value, Struct):
        return dict(value.as_mapping())
    return {}


def build_ctx(
    monorepo_root: Path | str | None = None,
    *,
    workspace_root: Path | str | None = None,
    commit: str | None = None,
    user: str | None = None,
    previous: Struct | None = None,
) -> object:
    """Build context struct with workspace git metadata and run metadata."""
    previous_workspace = _struct_fields(getattr(previous, "workspace", None))
    previous_run = _struct_fields(getattr(previous, "run", None))

    workspace_fields: dict[str, object] = {
        **previous_workspace,
        **get_git_info(monorepo_root),
        "directory": str(_default_workspace_directory(monorepo_root, workspace_root)),
    }
    if commit is not None:
        workspace_fields["commit"] = commit
    if user is not None:
        workspace_fields["user"] = user

    run_ctx = struct(
        id=str(previous_run.get("id", str(uuid.uuid4()))),
        user=str(previous_run.get("user", getpass.getuser())),
    )
    workspace_ctx = struct(**workspace_fields)
    return struct(workspace=workspace_ctx, run=run_ctx)


ctx = build_ctx()
