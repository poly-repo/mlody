"""Action primitive: build and push a container image."""

from __future__ import annotations

from pathlib import Path

from mlody.common.image_builder.log import collect_logs
from mlody.common.image_builder.output import SuccessResult
from mlody.common.image_builder.pipeline import PipelineInputs, run


def _is_insecure_registry(registry: str) -> bool:
    host = registry.split("/")[0].split(":")[0]
    return host in ("localhost", "127.0.0.1", "::1")


def build_image(args: dict) -> tuple[list[dict[str, object]], SuccessResult]:
    """Build and push a container image, returning (logs, result).

    logs is a list of structured log entries collected during the run.
    Nothing is printed to stderr while the action executes.
    On failure a BuilderError subclass is raised; logs emitted before the
    failure are discarded (not returned).
    """
    registry = args["registry"]
    inputs = PipelineInputs(
        targets=list(args["targets"]),
        sha=args["sha"],
        registry=registry,
        remote=args.get("remote"),
        cwd=Path(args.get("cwd", ".")),
        cache_root=Path(args["cache_root"]) if args.get("cache_root") else None,
        auth=None,
        dirty_policy=args.get("dirty_policy", "ignore"),
        base_image=args.get("base_image", "@debian_slim"),
        insecure=args.get("insecure", _is_insecure_registry(registry)),
    )
    with collect_logs() as logs:
        result = run(inputs)
    return logs, result
