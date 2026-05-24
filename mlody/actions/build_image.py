"""Action primitive: build and push a container image."""

from __future__ import annotations

from pathlib import Path

from mlody.common.image_builder.output import SuccessResult
from mlody.common.image_builder.pipeline import PipelineInputs, run


def build_image(args: dict) -> SuccessResult:
    inputs = PipelineInputs(
        targets=list(args["targets"]),
        sha=args["sha"],
        registry=args["registry"],
        remote=args.get("remote"),
        cwd=Path(args.get("cwd", ".")),
        cache_root=Path(args["cache_root"]) if args.get("cache_root") else None,
        auth=None,
        dirty_policy=args.get("dirty_policy", "ignore"),
        base_image=args.get("base_image", "@debian_slim"),
    )
    return run(inputs)
