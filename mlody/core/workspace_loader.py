"""Loading orchestration for the mlody Python workspace runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from mlody.core.registry_view import RegistryView
from mlody.core.workspace_models import RootInfo, WorkspaceLoadError

_logger = logging.getLogger(__name__)


class WorkspaceLoader:
    """Owns the Phase 1, Phase 2, and Phase 3 workspace loading flow."""

    def __init__(
        self,
        *,
        monorepo_root: Path,
        roots_file: Path,
        root_infos: MutableMapping[str, RootInfo],
        registry: RegistryView,
        extra_roots: Mapping[str, str],
        lazy_roots: Mapping[str, str],
        should_skip_mlody_file: Callable[[Path], bool],
        convert_ports_to_structs: Callable[[], None],
    ) -> None:
        self._monorepo_root = monorepo_root
        self._roots_file = roots_file
        self._root_infos = root_infos
        self._registry = registry
        self._extra_roots = extra_roots
        self._lazy_roots = lazy_roots
        self._should_skip_mlody_file = should_skip_mlody_file
        self._convert_ports_to_structs = convert_ports_to_structs

    def load(self) -> None:
        self._phase1_root_discovery()
        load_errors = self._phase2_full_evaluation()
        if load_errors:
            raise WorkspaceLoadError(load_errors)
        self._registry.resolve_all()
        self._convert_ports_to_structs()

    def _phase1_root_discovery(self) -> None:
        if self._roots_file.exists():
            self._registry.eval_file(self._roots_file)

        types_path = self._monorepo_root / "mlody" / "common" / "types.mlody"
        if not self._registry.is_loaded(types_path):
            try:
                self._registry.eval_file(types_path)
            except Exception:
                pass

        self._root_infos.clear()
        self._root_infos.update(self._registry.build_root_infos())

        for root_name, root_path in self._extra_roots.items():
            if root_name in self._root_infos:
                continue
            self._root_infos[root_name] = RootInfo(
                name=root_name,
                path=root_path,
                description="injected",
            )
            self._registry.ensure_root_placeholder(
                root_name,
                root_path,
                description="injected",
            )

        for root_name, root_path in self._lazy_roots.items():
            if self._registry.has_root(root_name):
                continue
            self._registry.ensure_root_placeholder(
                root_name,
                root_path,
                description="injected",
            )

    def _phase2_full_evaluation(self) -> list[tuple[Path, Exception]]:
        load_errors: list[tuple[Path, Exception]] = []
        for info in self._root_infos.values():
            root_abs = (self._monorepo_root / info.path.lstrip("/")).resolve()
            _logger.debug("Loading root: %s", root_abs)
            if not root_abs.is_dir():
                continue
            for mlody_file in sorted(root_abs.glob("**/*.mlody")):
                if self._should_skip_mlody_file(mlody_file):
                    _logger.debug("Skipping %s due to workspace skip list", mlody_file)
                    continue
                if self._registry.is_loaded(mlody_file):
                    continue
                try:
                    self._registry.eval_file(mlody_file)
                except Exception as exc:
                    _logger.error(
                        "Failed to load %s: %s: %s",
                        mlody_file,
                        type(exc).__name__,
                        exc,
                    )
                    load_errors.append((mlody_file, exc))
        return load_errors
