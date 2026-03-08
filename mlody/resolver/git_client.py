"""GitClient — thin subprocess abstraction over the git CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mlody.resolver.errors import GitNetworkError


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
        """Clone from the local monorepo using file:// transport.

        Uses --local to enable hardlinks (fast, no network). Checks out `sha`
        after cloning. Raises GitNetworkError on any subprocess failure.
        """
        url = f"file:///{self._root}"
        self._run(["git", "clone", "--local", "--no-checkout", url, str(dest)])
        # Fetch the specific SHA in case it's not a branch tip reachable by clone
        try:
            self._run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
        except GitNetworkError:
            # Fetch may fail if sha is already present; proceed to checkout
            pass
        self._run(["git", "-C", str(dest), "checkout", sha])

    def clone_remote(self, dest: Path, sha: str) -> None:
        """Clone from origin with minimal blob transfer.

        Uses --filter=blob:none to defer blob downloads until objects are
        accessed, keeping the initial clone fast. Raises GitNetworkError on
        any subprocess failure.
        """
        self._run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "origin", str(dest)]
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
