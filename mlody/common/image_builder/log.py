"""Structured logging for mlody-image-builder.

Default behaviour: emit one JSON object per line to stderr.

When a collect_logs() context is active, entries are appended to an
in-memory list instead and nothing is printed to stderr.  This allows
callers (e.g. actions.build_image) to capture the log stream and return
it to the caller rather than spilling it to the terminal.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_collector: ContextVar[list[dict[str, object]] | None] = ContextVar(
    "_collector", default=None
)


@contextmanager
def collect_logs() -> Generator[list[dict[str, object]], None, None]:
    """Redirect log output to an in-memory list for the duration of the block.

    No log lines are printed to stderr inside the block; each structured
    record is appended to the yielded list.  The collector is always
    removed on exit, even when an exception propagates.
    """
    entries: list[dict[str, object]] = []
    token = _collector.set(entries)
    try:
        yield entries
    finally:
        _collector.reset(token)


def log(level: str, phase: str, **fields: object) -> None:
    record: dict[str, object] = {"level": level, "phase": phase, **fields}
    active = _collector.get()
    if active is not None:
        active.append(record)
    else:
        print(json.dumps(record), file=sys.stderr)


def info(phase: str, **fields: object) -> None:
    log("info", phase, **fields)


def error(phase: str, **fields: object) -> None:
    log("error", phase, **fields)


def warn(phase: str, **fields: object) -> None:
    log("warn", phase, **fields)


def debug(phase: str, **fields: object) -> None:
    log("debug", phase, **fields)
