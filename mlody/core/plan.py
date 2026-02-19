"""Execution plan data model — stub activity types for future execution runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable


@runtime_checkable
class Activity(Protocol):
    """Protocol for all plan activities."""

    kind: str

    def to_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class BuildImage:
    """Stub: build a container image."""

    image_name: str = ""
    dockerfile: str = ""
    kind: str = "build_image"

    def to_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Execute:
    """Stub: execute a command."""

    command: str = ""
    kind: str = "execute"

    def to_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Plan:
    """Ordered list of activities representing an execution plan."""

    activities: list[Activity]

    def to_dict(self) -> list[dict[str, object]]:
        return [a.to_dict() for a in self.activities]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
