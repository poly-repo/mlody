"""Phase 0: resolve a symbolic git ref to a full 40-char SHA."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mlody.common.image_builder.errors import ResolveRefError
from mlody.common.image_builder.log import info


def resolve_ref(ref: str, cwd: Path) -> str:
    """Resolve any git ref to a full 40-char commit SHA.

    Accepts branch names, "HEAD", abbreviated SHAs, tags, or full SHAs.
    Runs ``git rev-parse --verify <ref>`` in cwd.

    Raises ResolveRefError if the ref cannot be resolved.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ResolveRefError(
            f"Cannot resolve ref {ref!r}: {result.stderr.strip()}",
            ref=ref,
        )
    sha = result.stdout.strip()
    info("ref", ref=ref, sha=sha)
    return sha
