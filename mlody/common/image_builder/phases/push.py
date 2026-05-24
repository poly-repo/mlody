"""Phase 5: OCI image push to the target registry using crane.

Uses Option B from the spec: invoke `crane push` as a subprocess against the
OCI image layout directory produced by `bazel build //_dynamic_image:image`.
The image layout is written to bazel-bin/_dynamic_image/image/ by rules_oci.

crane honours DOCKER_CONFIG for authentication, which is set by the RegistryAuth
abstraction. Credentials never appear in logs or error output.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mlody.common.image_builder.auth import RegistryAuth
from mlody.common.image_builder.errors import PushError
from mlody.common.image_builder.log import info

# rules_oci writes the OCI image layout to this path relative to the clone dir.
_IMAGE_LAYOUT_RELPATH = Path("bazel-bin") / "_dynamic_image" / "image"


@dataclasses.dataclass(frozen=True)
class PushResult:
    image_digest: str
    image_references: list[str]


def _infer_output_base(image_layout: Path) -> Path | None:
    """Infer Bazel output_base from a resolved layout path, if present."""
    resolved = image_layout.resolve(strict=False)
    parts = resolved.parts
    if "execroot" not in parts:
        return None
    idx = parts.index("execroot")
    if idx <= 0:
        return None
    return Path(*parts[:idx])


def _candidate_blob_sources(output_base: Path, digest: str) -> list[Path]:
    """Return likely blob locations for digest rehydration from Bazel caches."""
    candidates: list[Path] = []
    external_root = output_base / "external"
    if external_root.exists():
        # Most common shape for fetched OCI base repositories.
        candidates.extend(external_root.glob(f"*/blobs/sha256/{digest}"))
    # In many workspaces the OCI base layout is only materialized under execroot.
    execroot_root = output_base / "execroot" / "_main" / "bazel-out"
    if execroot_root.exists():
        candidates.extend(
            execroot_root.glob(f"*/bin/external/*/layout/blobs/sha256/{digest}")
        )
        candidates.extend(
            execroot_root.glob(f"*/bin/external/*/blobs/sha256/{digest}")
        )
    cas_blob = output_base / "cache" / "repos" / "v1" / "content_addressable" / "sha256" / digest
    candidates.append(cas_blob)
    # Remote CAS is shared across output_bases under ~/.cache/bazel/remote/cas.
    try:
        bazel_cache_root = output_base.parents[1]
        remote_cas_blob = bazel_cache_root / "remote" / "cas" / digest[:2] / digest
        candidates.append(remote_cas_blob)
    except IndexError:
        pass
    return [p for p in candidates if p.is_file()]


def _materialize_layout_for_push(image_layout: Path) -> tempfile.TemporaryDirectory[str]:
    """Create a self-contained OCI layout with regular files in blobs/sha256.

    rules_oci may emit blob symlinks that are invalid outside the action sandbox.
    We copy the image tree and replace blob symlinks with concrete file copies so
    crane can always read the layout.
    """
    tmpdir = tempfile.TemporaryDirectory(prefix="mlody-image-layout-")
    materialized_layout = Path(tmpdir.name) / "image"
    shutil.copytree(image_layout, materialized_layout, symlinks=True)

    output_base = _infer_output_base(image_layout)
    for writable_dir in (
        materialized_layout,
        materialized_layout / "blobs",
        materialized_layout / "blobs" / "sha256",
    ):
        if writable_dir.exists():
            writable_dir.chmod(writable_dir.stat().st_mode | 0o200)

    blob_dir = materialized_layout / "blobs" / "sha256"
    if not blob_dir.exists():
        return tmpdir

    for blob in blob_dir.iterdir():
        if not blob.is_symlink():
            continue

        source: Path | None = None
        original_blob = image_layout / "blobs" / "sha256" / blob.name
        try:
            source = blob.resolve(strict=True)
        except FileNotFoundError:
            try:
                source = original_blob.resolve(strict=True)
            except FileNotFoundError:
                source = None
            if output_base is not None:
                fallbacks = _candidate_blob_sources(output_base, blob.name)
                if source is None:
                    source = fallbacks[0] if fallbacks else None

        if source is None or not source.is_file():
            continue

        blob.unlink()
        shutil.copy2(source, blob)

    return tmpdir


def push_image(
    clone_dir: Path,
    registry: str,
    tags: list[str],
    auth: RegistryAuth,
    *,
    insecure: bool = False,
) -> PushResult:
    """Push the built OCI image to the registry with all derived tags.

    Invokes `crane push <image-layout-dir> <registry>:<tag>` for each tag.
    The image digest is extracted from crane's stdout (sha256:... line).
    Credentials are sourced exclusively from auth.env_vars().

    Raises PushError if bazel or crane is unavailable or if any tag push fails.
    """
    image_layout = clone_dir / _IMAGE_LAYOUT_RELPATH
    env = {**os.environ, **auth.env_vars()}
    image_references: list[str] = []
    digest: str | None = None

    materialized = _materialize_layout_for_push(image_layout)
    materialized_layout = Path(materialized.name) / "image"
    try:
        for tag in tags:
            reference = f"{registry}:{tag}"
            info("push", tag=tag, registry=registry)

            crane_flags = ["--insecure"] if insecure else []
            cmd = ["bazel", "run", "@multitool//tools/crane:crane", "--", "push", *crane_flags, str(materialized_layout), reference]
            result = subprocess.run(
                cmd,
                cwd=clone_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode != 0:
                raise PushError(
                    f"Push failed for tag {tag}",
                    tag=tag,
                    registry=registry,
                    returncode=result.returncode,
                    stderr=result.stderr.strip(),
                    stdout=result.stdout.strip(),
                    # Deliberately omit env from error context to protect credentials
                )

            image_references.append(reference)
            # crane push outputs "<ref>@sha256:<digest>" on the last stdout line.
            for line in result.stdout.splitlines():
                line = line.strip()
                if "@sha256:" in line:
                    digest = line.split("@sha256:", 1)[1]
                    if digest:
                        digest = "sha256:" + digest
    finally:
        materialized.cleanup()

    if digest is None:
        raise PushError(
            "Push completed but no image digest was returned",
            image_references=image_references,
        )

    info("push", status="success", digest=digest, references=image_references)
    return PushResult(image_digest=digest, image_references=image_references)
