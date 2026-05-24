"""Top-level pipeline orchestrator for mlody-image-builder."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from mlody.common.image_builder.auth import DockerConfigAuth, RegistryAuth
from mlody.common.image_builder.errors import BuilderError
from mlody.common.image_builder.output import SuccessResult
from mlody.common.image_builder.phases.build import run_bazel_build
from mlody.common.image_builder.phases.clone import DirtyPolicy, ensure_clone
from mlody.common.image_builder.phases.push import push_image
from mlody.common.image_builder.phases.remote import resolve_remote
from mlody.common.image_builder.phases.tags import derive_tags


@dataclasses.dataclass(frozen=True)
class PipelineInputs:
    targets: list[str]
    sha: str
    registry: str
    remote: str | None
    cwd: Path
    cache_root: Path | None
    auth: RegistryAuth | None
    dirty_policy: DirtyPolicy = "ignore"
    base_image: str = "@debian_slim"
    insecure: bool = False
    ref: str | None = None


def _derive_image_sha(commit_sha: str, applied_patch: str) -> str:
    """Return a stable 40-char SHA encoding both the commit and patch content.

    Used for tagging so two HEAD builds with different local changes produce
    distinct image identifiers even when they share the same commit SHA.
    """
    patch_sha256 = hashlib.sha256(applied_patch.encode()).hexdigest()
    return hashlib.sha256(f"{commit_sha}:{patch_sha256}".encode()).hexdigest()


def run(inputs: PipelineInputs) -> SuccessResult:
    """Execute the image-builder pipeline.

    Returns SuccessResult on full success.
    Raises BuilderError (or a subclass) on any phase failure.
    """
    auth: RegistryAuth = inputs.auth if inputs.auth is not None else DockerConfigAuth()

    # Phase 1: resolve git remote URL
    remote_url = resolve_remote(inputs.remote, inputs.cwd)

    # Phase 2: shallow clone at the pinned SHA, using cache if available
    clone_result = ensure_clone(
        inputs.sha, remote_url, inputs.cache_root, inputs.cwd, inputs.dirty_policy
    )

    # If a local patch was applied, derive an image SHA that encodes both the
    # commit SHA and the patch content.  This ensures two HEAD builds with
    # different local changes produce distinct tags even at the same commit.
    if clone_result.applied_patch:
        image_sha = _derive_image_sha(inputs.sha, clone_result.applied_patch)
    else:
        image_sha = inputs.sha

    embed_patch = inputs.ref == "HEAD"

    # Phase 3: build the combined OCI image target inside the clone
    run_bazel_build(
        image_sha,
        clone_result,
        inputs.targets,
        inputs.base_image,
        ref=inputs.ref,
        embed_patch=embed_patch,
    )

    # Phase 4: derive one OCI tag per input target (keyed by image_sha)
    tags = derive_tags(inputs.targets, image_sha)

    # Phase 5: push to registry with all derived tags
    push_result = push_image(
        clone_result.path,
        inputs.registry,
        tags,
        auth,
        insecure=inputs.insecure,
        monorepo_root=inputs.cwd,
    )

    return SuccessResult(
        image_digest=push_result.image_digest,
        image_references=push_result.image_references,
        commit_sha=inputs.sha,
        input_targets=inputs.targets,
        ref=inputs.ref,
        image_sha=image_sha,
    )
