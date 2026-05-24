"""Action primitive: run a built container image via docker run."""

from __future__ import annotations

import dataclasses
import subprocess

from mlody.common.image_builder.log import collect_logs, debug, error, info
from mlody.common.image_builder.output import SuccessResult


@dataclasses.dataclass(frozen=True)
class ExecuteResult:
    exit_code: int
    stdout: str
    stderr: str
    image_reference: str
    target: str


def _target_subdir(label: str) -> str:
    """Workspace-relative path for a Bazel target (mirrors build.py logic)."""
    label = label.lstrip("@")
    if "//" in label:
        label = label.split("//", 1)[1]
    if ":" in label:
        pkg, name = label.split(":", 1)
    else:
        pkg = label.rstrip("/")
        name = pkg.split("/")[-1]
    return f"{pkg}/{name}" if pkg else name


def _coerce_image(image: object) -> SuccessResult:
    """Accept SuccessResult or a plain dict (for shell ergonomics)."""
    if isinstance(image, SuccessResult):
        return image
    if isinstance(image, dict):
        return SuccessResult(
            image_digest=image["image_digest"],
            image_references=list(image["image_references"]),
            commit_sha=image["commit_sha"],
            input_targets=list(image["input_targets"]),
        )
    raise TypeError(f"image must be SuccessResult or dict, got {type(image).__name__}")


def _find_reference(image: SuccessResult, target: str) -> str:
    try:
        idx = image.input_targets.index(target)
        return image.image_references[idx]
    except (ValueError, IndexError):
        raise ValueError(
            f"Target {target!r} not in image metadata. "
            f"Available: {image.input_targets}"
        )


def execute(args: dict) -> tuple[list[dict[str, object]], ExecuteResult]:
    """Run a built container image via docker run, returning (logs, result).

    Required:
        image   — SuccessResult (or dict) from build_image
        target  — Bazel target; selects the image reference and infers the
                  binary path inside the container

    Optional:
        cmd     — list[str]: explicit command, overrides inferred binary path
        args    — list[str]: positional args appended after the binary
        env     — dict[str, str]: extra environment variables (-e KEY=VAL)
        remove  — bool: pass --rm to docker run (default True)
        network — str: --network value (e.g. "host")
    """
    image = _coerce_image(args["image"])
    target: str = args["target"]
    extra_args: list[str] = list(args.get("args") or [])
    env: dict[str, str] = dict(args.get("env") or {})
    remove: bool = args.get("remove", True)
    network: str | None = args.get("network")
    explicit_cmd: list[str] | None = list(args["cmd"]) if args.get("cmd") else None

    with collect_logs() as logs:
        reference = _find_reference(image, target)

        docker_cmd = ["docker", "run"]
        if remove:
            docker_cmd.append("--rm")
        if network:
            docker_cmd += ["--network", network]
        for key, val in env.items():
            docker_cmd += ["-e", f"{key}={val}"]
        docker_cmd.append(reference)

        if explicit_cmd is not None:
            docker_cmd.extend(explicit_cmd)
        else:
            docker_cmd.append("/" + _target_subdir(target))
        docker_cmd.extend(extra_args)

        debug("execute", action="docker_run", cmd=docker_cmd, reference=reference, target=target)
        proc = subprocess.run(docker_cmd, capture_output=True, text=True)

        if proc.returncode != 0:
            error(
                "execute",
                target=target,
                reference=reference,
                exit_code=proc.returncode,
                stderr=proc.stderr.strip(),
                stdout=proc.stdout.strip(),
            )
        else:
            info("execute", status="success", target=target, reference=reference, exit_code=proc.returncode)

        result = ExecuteResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            image_reference=reference,
            target=target,
        )

    return logs, result
