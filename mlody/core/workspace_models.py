"""Shared workspace models used by the runtime and its helper services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceLoadError(Exception):
    """One or more .mlody files failed to evaluate during Phase 2 loading."""

    def __init__(self, failures: list[tuple[Path, Exception]]) -> None:
        self.failures = failures
        lines = "\n".join(
            f"  {path}: {type(exc).__name__}: {exc}" for path, exc in failures
        )
        super().__init__(f"{len(failures)} file(s) failed to load:\n{lines}")


@dataclass(frozen=True)
class RootInfo:
    """Metadata for a registered root."""

    name: str
    path: str
    description: str
