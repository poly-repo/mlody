"""Action primitive: build and push a container image."""

from __future__ import annotations

from pathlib import Path

from mlody.common.image_builder.log import collect_logs
from mlody.common.image_builder.output import SuccessResult
from mlody.common.image_builder.phases.ref import resolve_ref
from mlody.common.image_builder.pipeline import PipelineInputs, run


def _is_insecure_registry(registry: str) -> bool:
    host = registry.split("/")[0].split(":")[0]
    return host in ("localhost", "127.0.0.1", "::1")


def build_image(args: dict) -> tuple[list[dict[str, object]], SuccessResult]:
    """Build and push a container image, returning (logs, result).

    args["sha"] accepts any git ref: branch name, "HEAD", abbreviated SHA, or
    full 40-char SHA.  It is resolved to a canonical full SHA before use.

    When args["sha"] == "HEAD", dirty_policy defaults to "apply" so local
    modifications and untracked files are included in the image, and the diff
    is embedded at /etc/mlody/local.patch inside the container.

    logs is a list of structured log entries collected during the run.
    Nothing is printed to stderr while the action executes.
    On failure a BuilderError subclass is raised.
    """
    registry = args["registry"]
    cwd = Path(args.get("cwd", "."))
    ref = args["sha"]

    with collect_logs() as logs:
        sha = resolve_ref(ref, cwd)
        dirty_policy = args.get("dirty_policy") or ("apply" if ref == "HEAD" else "ignore")
        inputs = PipelineInputs(
            targets=list(args["targets"]),
            sha=sha,
            registry=registry,
            remote=args.get("remote"),
            cwd=cwd,
            cache_root=Path(args["cache_root"]) if args.get("cache_root") else None,
            auth=None,
            dirty_policy=dirty_policy,
            base_image=args.get("base_image", "@debian_slim"),
            insecure=args.get("insecure", _is_insecure_registry(registry)),
            ref=ref,
        )
        result = run(inputs)

    return logs, result
