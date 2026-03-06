"""Workspace: two-phase loading of .mlody pipeline definitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.context import ctx as mlody_ctx
from mlody.core.targets import TargetAddress, parse_target, resolve_target_value

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RootInfo:
    """Metadata for a registered root."""

    name: str
    path: str
    description: str


class Workspace:
    """Wraps the starlarkish Evaluator with two-phase loading and target resolution."""

    def __init__(
        self,
        monorepo_root: Path,
        roots_file: Path | None = None,
        print_fn: Callable[..., None] = print,
    ) -> None:
        self._monorepo_root = monorepo_root
        self._roots_file = roots_file or (monorepo_root / "mlody" / "roots.mlody")
        self._evaluator = Evaluator(root=monorepo_root, print_fn=print_fn, extra_ctx=mlody_ctx)
        self._root_infos: dict[str, RootInfo] = {}

    @property
    def evaluator(self) -> Evaluator:
        return self._evaluator

    @property
    def root_infos(self) -> dict[str, RootInfo]:
        return self._root_infos

    def load(self) -> None:
        """Execute two-phase loading of pipeline definitions."""
        # Phase 1: Root discovery
        if not self._roots_file.exists():
            msg = f"Roots file not found: {self._roots_file}"
            raise FileNotFoundError(msg)

        self._evaluator.eval_file(self._roots_file)

        self._root_infos = {}
        for _key, root_obj in self._evaluator.roots.items():
            name = root_obj.name
            self._root_infos[name] = RootInfo(
                name=name,
                path=getattr(root_obj, "path", ""),
                description=getattr(root_obj, "description", ""),
            )

        # Phase 2: Full evaluation
        for info in self._root_infos.values():
            root_abs = self._monorepo_root / info.path.lstrip("/")
            _logger.debug("Loading root: %s", root_abs)
            if not root_abs.is_dir():
                continue
            for mlody_file in sorted(root_abs.glob("**/*.mlody")):
                if mlody_file in self._evaluator.loaded_files:
                    continue
                self._evaluator.eval_file(mlody_file)

    def resolve(self, target: str | TargetAddress) -> object:
        """Parse (if string) and resolve a target to a value."""
        address = parse_target(target) if isinstance(target, str) else target
        return resolve_target_value(address, self._evaluator._roots_by_name)
