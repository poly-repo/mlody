"""Loading orchestration for the mlody Python workspace runtime."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.python.starlarkish.core.struct import Struct

from mlody.core.registry_view import RegistryView
from mlody.core.value_context_validation import (
    validate_context_restricted_values_registry,
)
from mlody.core.workspace_models import RootInfo, WorkspaceLoadError

_logger = logging.getLogger(__name__)
_SYNTHETIC_MAV_USER_KEY = ("user", "", "mav")


def _noop_print(*_args: object, **_kwargs: object) -> None:
    """Discard all output — used as the default reporter print_fn."""


@dataclass
class _LoaderReporter:
    """Minimal reporter for WorkspaceLoader — carries print_fn and verbose flag.

    Structurally compatible with mlody.resolver.resolver.Reporter; callers
    can pass a Reporter instance directly since both have the same fields.
    """

    print_fn: Callable[..., None]
    console: object | None = None
    verbose: bool = False


_NOOP_LOADER_REPORTER = _LoaderReporter(print_fn=_noop_print)


class WorkspaceLoader:
    """Owns the Phase 1, Phase 2, and Phase 3 workspace loading flow."""

    def __init__(
        self,
        *,
        monorepo_root: Path,
        workspace_root: Path | None = None,
        roots_file: Path,
        root_infos: MutableMapping[str, RootInfo],
        registry: RegistryView,
        extra_roots: Mapping[str, str],
        lazy_roots: Mapping[str, str],
        should_skip_mlody_file: Callable[[Path], bool],
        convert_ports_to_structs: Callable[[], None],
        resolve_value_sources: Callable[[], None],
        after_root_discovery: Callable[[], None] | None = None,
        reporter: Any | None = None,
        extra_eval_files: list[Path] | None = None,
    ) -> None:
        self._monorepo_root = monorepo_root
        self._workspace_root = workspace_root if workspace_root is not None else monorepo_root
        self._roots_file = roots_file
        self._root_infos = root_infos
        self._registry = registry
        self._extra_roots = extra_roots
        self._lazy_roots = lazy_roots
        self._should_skip_mlody_file = should_skip_mlody_file
        self._convert_ports_to_structs = convert_ports_to_structs
        self._resolve_value_sources = resolve_value_sources
        self._after_root_discovery = after_root_discovery
        # Accept any object with .verbose and .print_fn (duck-typed).
        # This avoids a circular import with mlody.resolver.resolver.
        self._reporter: Any = reporter if reporter is not None else _NOOP_LOADER_REPORTER
        self._last_phase2_files_loaded: int = 0
        self._extra_eval_files: list[Path] = extra_eval_files or []

    def load(self, *, workspace: object | None = None) -> None:
        verbose = self._reporter.verbose
        print_fn = self._reporter.print_fn

        if verbose:
            print_fn("[mlody] phase 1: root discovery started")
        t0 = time.monotonic()
        self._phase1_root_discovery()
        elapsed1 = time.monotonic() - t0
        root_count = len(self._root_infos)
        if verbose:
            print_fn(
                f"[mlody] phase 1: root discovery done ({root_count} roots)"
                f" [{elapsed1:.2f}s]"
            )

        if self._after_root_discovery is not None:
            self._after_root_discovery()

        if verbose:
            print_fn(f"[mlody] phase 2: full evaluation started ({root_count} roots)")
        t2 = time.monotonic()
        load_errors = self._phase2_full_evaluation(verbose=verbose, print_fn=print_fn)
        elapsed2 = time.monotonic() - t2
        files_loaded = self._last_phase2_files_loaded
        if verbose:
            print_fn(
                f"[mlody] phase 2: done ({files_loaded} files loaded,"
                f" {len(load_errors)} errors) [{elapsed2:.2f}s]"
            )

        if load_errors:
            raise WorkspaceLoadError(load_errors)
        self._eval_extra_files()
        self._ensure_synthetic_mav_user()
        self._registry.resolve_all()
        self._convert_ports_to_structs()
        self._resolve_value_sources()
        validate_context_restricted_values_registry(self._registry)

    def _eval_extra_files(self) -> None:
        for actual_path in self._extra_eval_files:
            virtual_path = self._monorepo_root / actual_path.name
            self._registry.register_path_redirect(virtual_path, actual_path)
            self._registry.eval_file(virtual_path)

    def _phase1_root_discovery(self) -> None:
        if self._roots_file.exists():
            self._registry.eval_file(self._roots_file)

        mm_path = self._monorepo_root / "mlody" / "common" / "mm.mlody"
        if self._roots_file.exists():
            # Load mm.mlody before types.mlody so the MmNamespace singleton is
            # already initialized when types.mlody calls register_mm_pattern().
            # Auto-generated constructors (e.g. mm.vector) are registered from
            # typedef() via rule.mlody → register_mm_pattern; they arrive after
            # mm.mlody runs, so the namespace must exist first.
            #
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

        types_path = self._monorepo_root / "mlody" / "common" / "types.mlody"
        if not self._registry.is_loaded(types_path):
            try:
                self._registry.eval_file(types_path)
            except Exception:
                pass

        render_path = self._monorepo_root / "mlody" / "common" / "render.mlody"
        if self._roots_file.exists():
            if not self._registry.is_loaded(render_path):
                self._registry.eval_file(render_path)
            self._registry.propagate_globals_as_persistent_injections(
                render_path,
                ["render_value", "stage_value", "render_element_preview", "render_element"],
            )

        config_path = self._monorepo_root / "mlody" / "common" / "config.mlody"
        if self._roots_file.exists():
            if not self._registry.is_loaded(config_path):
                self._registry.eval_file(config_path)
            self._registry.propagate_globals_as_persistent_injections(
                config_path, ["config"]
            )

        if self._roots_file.exists():
            try:
                from mlody.starlark import make_actions_struct  # noqa: PLC0415

                self._registry.inject_persistent("actions", make_actions_struct())
            except ImportError:
                pass

        workspace_path = self._workspace_root / "workspace.mlody"
        if workspace_path.exists() and not self._registry.is_loaded(workspace_path):
            self._registry.eval_file(workspace_path)

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

    def _phase2_full_evaluation(
        self,
        *,
        verbose: bool = False,
        print_fn: Callable[..., None] = _noop_print,
    ) -> list[tuple[Path, Exception]]:
        load_errors: list[tuple[Path, Exception]] = []
        files_loaded = 0
        for info in self._root_infos.values():
            root_abs = (self._monorepo_root / info.path.lstrip("/")).resolve()
            _logger.debug("Loading root: %s", root_abs)
            if not root_abs.is_dir():
                continue
            for mlody_file in sorted(root_abs.glob("**/*.mlody")):
                if (
                    self._workspace_root != self._monorepo_root
                    and mlody_file == self._monorepo_root / "workspace.mlody"
                ):
                    continue
                if self._should_skip_mlody_file(mlody_file):
                    _logger.debug("Skipping %s due to workspace skip list", mlody_file)
                    continue
                if self._registry.is_loaded(mlody_file):
                    continue
                try:
                    relative_path: str
                    try:
                        relative_path = str(mlody_file.relative_to(self._monorepo_root))
                    except ValueError:
                        relative_path = str(mlody_file)
                    if verbose:
                        print_fn(f"[mlody]   loading {relative_path}")
                    self._registry.eval_file(mlody_file)
                    files_loaded += 1
                except Exception as exc:
                    _logger.error(
                        "Failed to load %s: %s: %s",
                        mlody_file,
                        type(exc).__name__,
                        exc,
                    )
                    load_errors.append((mlody_file, exc))
        self._last_phase2_files_loaded = files_loaded
        return load_errors

    def _ensure_synthetic_mav_user(self) -> None:
        for key, _value in self._registry.iter_registry_items():
            if (
                len(key) == 3
                and key[0] == _SYNTHETIC_MAV_USER_KEY[0]
                and key[2] == _SYNTHETIC_MAV_USER_KEY[2]
            ):
                return

        user_fields: dict[str, object] = {
            "kind": "user",
            "name": "mav",
            "description": "Maurizio Vitale",
            "groups": ["admin"],
            "avatar": "assets/images/avatars/avatars-4-2.png",
        }
        entity_type = self._registry.type_by_name("mlody-user")
        if entity_type is not None:
            user_fields["_entity_type"] = entity_type
        self._registry.set_registry_entity(_SYNTHETIC_MAV_USER_KEY, Struct(**user_fields))
