"""Workspace: two-phase loading of .mlody pipeline definitions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.syntax import Syntax

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.context import ctx as mlody_ctx
from mlody.core.targets import TargetAddress, parse_target, resolve_target_value

_logger = logging.getLogger(__name__)


class WorkspaceLoadError(Exception):
    """One or more .mlody files failed to evaluate during Phase 2 loading."""

    def __init__(self, failures: list[tuple[Path, Exception]]) -> None:
        self.failures = failures
        lines = "\n".join(
            f"  {path}: {type(exc).__name__}: {exc}"
            for path, exc in failures
        )
        super().__init__(f"{len(failures)} file(s) failed to load:\n{lines}")


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
        console: Console | None = None,
    ) -> None:
        self._monorepo_root = monorepo_root
        self._roots_file = roots_file or (monorepo_root / "mlody" / "roots.mlody")
        self._console = console if console is not None else Console()
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
        load_errors: list[tuple[Path, Exception]] = []
        for info in self._root_infos.values():
            root_abs = self._monorepo_root / info.path.lstrip("/")
            _logger.debug("Loading root: %s", root_abs)
            if not root_abs.is_dir():
                continue
            for mlody_file in sorted(root_abs.glob("**/*.mlody")):
                if mlody_file in self._evaluator.loaded_files:
                    continue
                try:
                    self._evaluator.eval_file(mlody_file)
                except Exception as exc:
                    _logger.error(
                        "Failed to load %s: %s: %s", mlody_file, type(exc).__name__, exc
                    )
                    load_errors.append((mlody_file, exc))

        if load_errors:
            raise WorkspaceLoadError(load_errors)

        self._evaluator.resolve()
        data = {k: v.to_dict() if hasattr(v, "to_dict") else v for k, v in self._evaluator.all.items()}
        self._console.print(Syntax(json.dumps(data, indent=2, default=repr), "json"))

    def resolve(self, target: str | TargetAddress) -> object:
        """Parse (if string) and resolve a target to a value."""
        address = parse_target(target) if isinstance(target, str) else target
        return resolve_target_value(address, self._evaluator._roots_by_name)
