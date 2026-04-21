# This is python, not starlark

from pathlib import Path
import getpass
import os
import uuid

import git
from common.python.starlarkish.core.struct import struct


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


def build_ctx(monorepo_root: Path | str | None = None) -> object:
    """Build context struct with workspace git metadata and run metadata."""
    workspace_ctx = struct(**get_git_info(monorepo_root))
    run_ctx = struct(id=str(uuid.uuid4()), user=getpass.getuser())
    return struct(workspace=workspace_ctx, run=run_ctx)


ctx = build_ctx()
