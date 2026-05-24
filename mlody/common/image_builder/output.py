"""JSON result serialization for mlody-image-builder.

All result output (success and error) goes exclusively to stdout.
"""

from __future__ import annotations

import dataclasses
import json
import sys


@dataclasses.dataclass(frozen=True)
class SuccessResult:
    image_digest: str
    image_references: list[str]
    commit_sha: str
    input_targets: list[str]
    ref: str | None = None
    image_sha: str | None = None
    local_patch_sha: str | None = None


def emit_success(result: SuccessResult) -> None:
    """Print JSON success payload to stdout."""
    payload = dataclasses.asdict(result)
    print(json.dumps(payload, indent=2))


def emit_error(
    error_type: str,
    message: str,
    context: dict[str, object],
) -> None:
    """Print JSON error payload to stdout."""
    payload: dict[str, object] = {
        "error": error_type,
        "message": message,
        **context,
    }
    print(json.dumps(payload, indent=2))
