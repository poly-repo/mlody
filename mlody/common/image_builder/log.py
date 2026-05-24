"""Structured stderr logging for mlody-image-builder.

Emits one JSON object per line to stderr. Never touches stdout.
No rich spinners or progress bars.
"""

from __future__ import annotations

import json
import sys


def log(level: str, phase: str, **fields: object) -> None:
    """Emit a structured JSON log line to stderr."""
    record = {"level": level, "phase": phase, **fields}
    print(json.dumps(record), file=sys.stderr)


def info(phase: str, **fields: object) -> None:
    """Emit an info-level structured log line to stderr."""
    log("info", phase, **fields)


def error(phase: str, **fields: object) -> None:
    """Emit an error-level structured log line to stderr."""
    log("error", phase, **fields)


def debug(phase: str, **fields: object) -> None:
    """Emit a debug-level structured log line to stderr."""
    log("debug", phase, **fields)
