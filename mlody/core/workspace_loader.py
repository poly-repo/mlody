"""Loading orchestration for the mlody Python workspace runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from mlody.core.registry_view import RegistryView
from mlody.core.value_context_validation import (
    validate_context_restricted_values_registry,
)
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
        after_root_discovery: Callable[[], None] | None = None,
    ) -> None:
        self._monorepo_root = monorepo_root
        self._roots_file = roots_file
        self._root_infos = root_infos
        self._registry = registry
        self._extra_roots = extra_roots
        self._lazy_roots = lazy_roots
        self._should_skip_mlody_file = should_skip_mlody_file
        self._convert_ports_to_structs = convert_ports_to_structs
        self._after_root_discovery = after_root_discovery

    def load(self, *, workspace: object | None = None) -> None:
        self._phase1_root_discovery()
        if self._after_root_discovery is not None:
            self._after_root_discovery()
        load_errors = self._phase2_full_evaluation()
        if load_errors:
            raise WorkspaceLoadError(load_errors)
        self._registry.resolve_all()
        self._convert_ports_to_structs()
        validate_context_restricted_values_registry(self._registry)

    def _phase1_root_discovery(self) -> None:
        if self._roots_file.exists():
            self._registry.eval_file(self._roots_file)

        types_path = self._monorepo_root / "mlody" / "common" / "types.mlody"
        if not self._registry.is_loaded(types_path):
            try:
                self._registry.eval_file(types_path)
            except Exception:
                pass

        mm_path = self._monorepo_root / "mlody" / "common" / "mm.mlody"
        if self._roots_file.exists():
            # Only load mm.mlody in a proper mlody workspace (one with roots.mlody).
            # Sandboxes or test fixtures without a roots file are exempt.
            # When roots.mlody is present, mm.mlody is mandatory — raise if absent.
            if not self._registry.is_loaded(mm_path):
                self._registry.eval_file(mm_path)
            # Always propagate even when mm.mlody was pre-loaded by a caller,
            # so that render.mlody and user files evaluated later see mm/defmethod.
            self._registry.propagate_globals_as_persistent_injections(
                mm_path, ["mm", "defmethod"]
            )

        render_path = self._monorepo_root / "mlody" / "common" / "render.mlody"
        if self._roots_file.exists():
            if not self._registry.is_loaded(render_path):
                self._registry.eval_file(render_path)
            self._registry.propagate_globals_as_persistent_injections(
                render_path, ["render_value", "render_element_preview", "render_element"]
            )

        config_path = self._monorepo_root / "mlody" / "common" / "config.mlody"
        if self._roots_file.exists():
            if not self._registry.is_loaded(config_path):
                self._registry.eval_file(config_path)
            self._registry.propagate_globals_as_persistent_injections(
                config_path, ["config"]
            )

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
