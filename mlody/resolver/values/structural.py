"""Structural MlodyValue subclasses: workspace, folder, source, unresolved, vector, source-range.

These are the "structural" values that represent filesystem entities and
traversal result containers.  None of them carry rendering logic — renderers
live in ``mlody.resolver.render``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mlody.resolver.values.base import MlodyValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


@dataclass(frozen=True)
class MlodyWorkspaceValue(MlodyValue):
    """The workspace itself (label has no entity spec).

    ``name`` is the workspace name (``label.workspace``), or ``None`` for CWD.
    ``root`` is the absolute filesystem path of the monorepo root.
    """

    name: str | None
    root: str


@dataclass(frozen=True)
class MlodyFolderValue(MlodyValue):
    """A directory on disk under the workspace.

    ``path`` is workspace-relative (without leading slash), matching the label.
    ``children`` contains the names of the immediate directory entries.
    """

    path: str
    children: list[str]  # pyright: ignore[reportMutableClassVariable]


@dataclass(frozen=True)
class MlodySourceValue(MlodyValue):
    """A ``.mlody`` source file on disk.

    ``path`` is the workspace-relative path **without** the ``.mlody`` suffix,
    matching the label exactly.
    ``abs_path`` is the absolute filesystem path to the ``.mlody`` file,
    used for content display.
    """

    path: str
    abs_path: Path | None = None


@dataclass(frozen=True)
class MlodyUnresolvedValue(MlodyValue):
    """Soft-failure sentinel.

    Returned (never raised) when any resolution step cannot proceed.
    ``reason`` is a human-readable string naming the failed step.
    """

    label: "Label"
    reason: str


@dataclass(frozen=True)
class MlodyVectorValue(MlodyValue):
    """A collection of ``MlodyValue`` elements produced by wildcard or recursive-descent traversal.

    ``elements`` is a tuple of ``MlodyValue`` instances in deterministic order
    (declaration order for wildcards, depth-first for recursive descent).
    """

    elements: tuple[MlodyValue, ...]


@dataclass(frozen=True)
class MlodySourceRangeValue(MlodyValue):
    """A resolved source-range attribute: file path + line span."""

    filepath: str
    abs_path: Path
    start_line: int
    end_line: int
