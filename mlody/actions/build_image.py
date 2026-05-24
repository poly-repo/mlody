"""Action primitive: build and push a container image."""

from __future__ import annotations

from pathlib import Path

from mlody.common.image_builder.output import SuccessResult
from mlody.common.image_builder.pipeline import PipelineInputs, run


def _is_insecure_registry(registry: str) -> bool:
    host = registry.split("/")[0].split(":")[0]
    return host in ("localhost", "127.0.0.1", "::1")


def build_image(args: dict) -> SuccessResult:
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
    return run(inputs)
