"""GitClient — thin subprocess abstraction over the git CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mlody.resolver.errors import GitNetworkError

# Top-level directories to include in the sparse checkout. Add an entry here
# to extend the sparse checkout to a new monorepo subtree — no changes to
# clone logic are needed.
SPARSE_INCLUDE: list[str] = ["mlody"]

# Sub-paths within included directories to exclude. Add an entry here to drop
# a subtree that is irrelevant to value resolution (e.g. large binary assets).
SPARSE_EXCLUDE: list[str] = ["mlody/docs"]


class GitClient:
    """Wraps git CLI calls with typed inputs and outputs.

    All methods pass arguments as list elements to subprocess.run — no shell
    interpolation or shell=True is used anywhere in this class. This prevents
    shell injection when committoids or paths come from user input.
    """

    def __init__(self, monorepo_root: Path) -> None:
        self._root = monorepo_root

    def _run(self, cmd: list[str], *, cwd: Path | None = None) -> str:
        """Run a git command, return stdout stripped, or raise GitNetworkError."""
        effective_cwd = cwd or self._root
        result = subprocess.run(
            cmd,
            cwd=effective_cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitNetworkError(
                command=cmd,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result.stdout.strip()

    def ls_remote(self) -> list[tuple[str, str]]:
        """Run `git ls-remote origin` and return (sha, ref) pairs.

        The full unfiltered output is returned — callers perform their own
        filtering so that branch/tag collision detection sees all ref types.
        """
        stdout = self._run(["git", "ls-remote", "origin"])
        pairs: list[tuple[str, str]] = []
        for line in stdout.splitlines():
            parts = line.split("\t", maxsplit=1)
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return pairs

    def cat_file_type(self, sha: str) -> str | None:
        """Return the object type for `sha`, or None if not present locally.

        Uses git cat-file -t which exits non-zero when the object is unknown,
        so a non-zero exit is not an error condition here — just means the
        commit is absent locally.
        """
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=self._root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def clone_local(self, dest: Path, sha: str) -> None:
        """Clone from the local monorepo using file:// transport with sparse checkout.

        Uses --local to enable hardlinks (fast, no network). Applies the same
        sparse-checkout patterns as clone_remote so that only the directories
        declared in SPARSE_INCLUDE (minus SPARSE_EXCLUDE) are checked out.
        Raises GitNetworkError on any subprocess failure.
        """
        patterns: list[str] = [f"/{d}/" for d in SPARSE_INCLUDE] + [
            f"!/{e}/" for e in SPARSE_EXCLUDE
        ]
        url = f"file:///{self._root}"
        self._run(["git", "clone", "--local", "--no-checkout", url, str(dest)])
        self._run(
            ["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone"] + patterns
        )
        # Fetch the specific SHA in case it's not a branch tip reachable by clone
        try:
            self._run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
        except GitNetworkError:
            # Fetch may fail if sha is already present; proceed to checkout
            pass
        self._run(["git", "-C", str(dest), "checkout", sha])

    def clone_remote(self, dest: Path, sha: str) -> None:
        """Clone from origin with minimal blob and tree transfer.

        Uses --filter=blob:none --sparse to defer blob downloads and limit
        tree traversal to the directories listed in SPARSE_INCLUDE (minus
        SPARSE_EXCLUDE). This keeps the initial clone fast even on large
        monorepos. Raises GitNetworkError on any subprocess failure.
        """
        patterns: list[str] = [f"/{d}/" for d in SPARSE_INCLUDE] + [
            f"!/{e}/" for e in SPARSE_EXCLUDE
        ]
        self._run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--sparse",
                "origin",
                str(dest),
            ]
        )
        self._run(
            ["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone"] + patterns
        )
        self._run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
        self._run(["git", "-C", str(dest), "checkout", sha])

    def local_remote_tracking_refs(self) -> list[tuple[str, str]]:
        """Return (sha, ref) pairs from local remote-tracking refs for origin.

        Uses `git for-each-ref refs/remotes/origin` so it works offline and
        finds branches that existed on origin but were since deleted (e.g.
        merged branches). Returns refs stripped to the short name under
        refs/heads/ convention — e.g. refs/remotes/origin/main becomes
        refs/heads/main — so callers can use the same matching logic as
        ls_remote output.
        """
        try:
            stdout = self._run(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(objectname) %(refname)",
                    "refs/remotes/origin",
                ]
            )
        except GitNetworkError:
            return []
        pairs: list[tuple[str, str]] = []
        for line in stdout.splitlines():
            parts = line.split(" ", maxsplit=1)
            if len(parts) == 2:
                sha, refname = parts
                # Strip refs/remotes/origin/ prefix, remap to refs/heads/<name>
                prefix = "refs/remotes/origin/"
                if refname.startswith(prefix):
                    short = refname[len(prefix):]
                    if short == "HEAD":
                        continue
                    pairs.append((sha, f"refs/heads/{short}"))
        return pairs

    def remote_url(self) -> str:
        """Return the URL of the origin remote."""
        return self._run(["git", "remote", "get-url", "origin"])
