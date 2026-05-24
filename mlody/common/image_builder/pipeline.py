"""Top-level pipeline orchestrator for mlody-image-builder."""

from __future__ import annotations

import dataclasses
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


def run(inputs: PipelineInputs) -> SuccessResult:
    """Execute the five-phase image-builder pipeline.

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

    # Phase 3: build the combined OCI image target inside the clone
    run_bazel_build(inputs.sha, clone_result, inputs.targets, inputs.base_image)

    # Phase 4: derive one OCI tag per input target
    tags = derive_tags(inputs.targets, inputs.sha)

    # Phase 5: push to registry with all derived tags
    push_result = push_image(clone_result.path, inputs.registry, tags, auth, insecure=inputs.insecure)

    return SuccessResult(
        image_digest=push_result.image_digest,
        image_references=push_result.image_references,
        commit_sha=inputs.sha,
        input_targets=inputs.targets,
    )
