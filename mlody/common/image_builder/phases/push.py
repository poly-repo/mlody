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
from mlody.common.image_builder.log import debug, error, info, warn

# rules_oci writes the OCI image layout to this path relative to the clone dir.
_IMAGE_LAYOUT_RELPATH = Path("bazel-bin") / "_dynamic_image" / "image"


def _classify_push_failure(stderr: str, stdout: str) -> str:
    """Return a short category label for a crane push failure.

    Inspects stderr and stdout (which may contain bazel build output) to
    distinguish the most actionable root causes without requiring callers to
    parse free-form text.
    """
    combined = (stderr + "\n" + stdout).lower()
    if any(k in combined for k in ("connection refused", "dial tcp", "no such host", "i/o timeout", "eof")):
        return "registry_unreachable"
    if any(k in combined for k in ("401", "403", "unauthorized", "denied", "forbidden")):
        return "auth_failure"
    if "could not parse reference" in combined:
        return "invalid_reference"
    # Bazel-level failures: build errors or target not found appear on stdout.
    if any(k in combined for k in ("error: no such target", "build failed", "error: failed to run")):
        return "crane_unavailable"
    if "no such file or directory" in combined or "exec format error" in combined:
        return "crane_unavailable"
    return "unknown"


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
            symlink_target = blob.readlink() if blob.is_symlink() else None
            warn(
                "push",
                action="unresolvable_blob",
                blob=blob.name,
                symlink_target=str(symlink_target),
                output_base=str(output_base),
            )
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
    monorepo_root: Path | None = None,
) -> PushResult:
    """Push the built OCI image to the registry with all derived tags.

    Invokes `crane push <image-layout-dir> <registry>:<tag>` for each tag.
    The image digest is extracted from crane's stdout (sha256:... line).
    Credentials are sourced exclusively from auth.env_vars().

    crane is invoked via `bazel run @multitool//tools/crane:crane` from
    monorepo_root when provided (warm cache), falling back to clone_dir.

    Raises PushError if bazel or crane is unavailable or if any tag push fails.
    """
    image_layout = clone_dir / _IMAGE_LAYOUT_RELPATH
    env = {**os.environ, **auth.env_vars()}
    image_references: list[str] = []
    digest: str | None = None
    bazel_cwd = monorepo_root if monorepo_root is not None else clone_dir

    materialized = _materialize_layout_for_push(image_layout)
    materialized_layout = Path(materialized.name) / "image"
    try:
        for tag in tags:
            # registry may already contain a path (e.g. "ghcr.io/org/repo") in
            # which case :<tag> is the tag separator.  When registry is just
            # host:port (e.g. "localhost:5001") there is no path component and
            # "host:port:tag" is an invalid OCI reference; use the tag as the
            # image name instead: "host:port/tag".
            if "/" in registry:
                reference = f"{registry}:{tag}"
            else:
                reference = f"{registry}/{tag}"
            info("push", tag=tag, registry=registry)

            crane_flags = ["--insecure"] if insecure else []
            cmd = ["bazel", "run", "@multitool//tools/crane:crane", "--", *crane_flags, "push", str(materialized_layout), reference]
            debug("push", action="crane_invoke", cmd=cmd, cwd=str(bazel_cwd), reference=reference, layout=str(materialized_layout))
            result = subprocess.run(
                cmd,
                cwd=bazel_cwd,
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                failure_kind = _classify_push_failure(stderr, stdout)
                error(
                    "push",
                    tag=tag,
                    registry=registry,
                    returncode=result.returncode,
                    failure_kind=failure_kind,
                    crane_stderr=stderr,
                    crane_stdout=stdout,
                )
                raise PushError(
                    f"Push failed for tag {tag} ({failure_kind})",
                    tag=tag,
                    registry=registry,
                    returncode=result.returncode,
                    stderr=stderr,
                    stdout=stdout,
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
