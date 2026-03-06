# This is python, not starlark

import uuid
import git
from common.python.starlarkish.core.struct import struct
import getpass
import os


def get_git_info():
    """Retrieves git information for the current repository."""
    try:
        repo = git.Repo(os.getcwd(), search_parent_directories=True)

        latest_commit = repo.head.commit
        return {
            "branch": repo.git.rev_parse("--abbrev-ref", "HEAD"),
            "commit": latest_commit.hexsha,
        }

    except Exception as e:
        print(f"Error: {e}")
        return {}


workspace_ctx = struct(**get_git_info())
run_ctx = struct(id=str(uuid.uuid4()), user=getpass.getuser())

ctx = struct(workspace=workspace_ctx, run=run_ctx)
