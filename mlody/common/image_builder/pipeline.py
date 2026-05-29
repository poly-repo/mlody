"""Top-level pipeline orchestrator for mlody-image-builder."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from mlody.common.hash import combine_commit_and_patch_sha
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


def _patch_file_content(applied_patch: str, applied_untracked: list[str]) -> str:
    """Canonical content for local.patch — must match what build.py writes."""
    return applied_patch


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

    # If local changes were applied (tracked or untracked), derive an image SHA
    # that encodes both the commit SHA and the full patch content.  This ensures
    # two HEAD builds with different local changes produce distinct tags even at
    # the same commit.  local_patch_sha is the SHA256 of the local.patch file
    # content (None when no changes were applied).
    has_local_changes = bool(clone_result.applied_patch or clone_result.applied_untracked)
    if has_local_changes:
        patch_content = _patch_file_content(clone_result.applied_patch, clone_result.applied_untracked)
        local_patch_sha: str | None = hashlib.sha256(patch_content.encode()).hexdigest()
        image_sha = combine_commit_and_patch_sha(inputs.sha, local_patch_sha)
    else:
        local_patch_sha = None
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
        local_patch_sha=local_patch_sha,
    )
