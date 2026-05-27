"""Focused tests for the workspace loading orchestration service."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyfakefs.fake_filesystem import FakeFilesystem
import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.workspace_loader import WorkspaceLoader
from mlody.core.workspace_models import RootInfo, WorkspaceLoadError
from mlody.core.value_context_validation import (
    ContextRestrictedValueValidationError,
)
from mlody.resolver.resolver import Reporter, WorkspaceRequest, _make_workspace_request, get_or_build_baseline_workspace, evict_baseline_workspace


@dataclass
class _FakeRegistry:
    root_infos_to_return: dict[str, RootInfo]
    eval_failures: dict[Path, Exception] = field(default_factory=dict)
    registry_items: tuple[tuple[tuple[object, object, object], object], ...] = ()
    type_names_to_return: dict[str, object] = field(default_factory=dict)
    loaded_files: set[Path] = field(default_factory=set)
    eval_calls: list[Path] = field(default_factory=list)
    propagated_injections: list[tuple[Path, list[str]]] = field(default_factory=list)
    placeholders: list[tuple[str, str, str]] = field(default_factory=list)
    registry_entities_set: list[tuple[tuple[object, object, object], object]] = field(
        default_factory=list
    )
    resolved: bool = False
    configs_to_return: list[tuple[str, object]] = field(default_factory=list)
    persistent_injections: dict[str, object] = field(default_factory=dict)

    def eval_file(self, file_path: Path) -> None:
        self.eval_calls.append(file_path)
        if file_path in self.eval_failures:
            raise self.eval_failures[file_path]
        self.loaded_files.add(file_path)

    def is_loaded(self, file_path: Path) -> bool:
        return file_path in self.loaded_files

    def build_root_infos(self) -> dict[str, RootInfo]:
        return dict(self.root_infos_to_return)

    def ensure_root_placeholder(
        self,
        root_name: str,
        root_path: str,
        *,
        description: str = "injected",
    ) -> None:
        self.placeholders.append((root_name, root_path, description))

    def has_root(self, root_name: str) -> bool:
        return any(name == root_name for name, _path, _desc in self.placeholders)

    def resolve_all(self) -> None:
        self.resolved = True

    def propagate_globals_as_persistent_injections(
        self, file_path: Path, names: list[str]
    ) -> None:
        self.propagated_injections.append((file_path, list(names)))

    def inject_persistent(self, name: str, value: object) -> None:
        self.persistent_injections[name] = value

    def iter_registry_items(
        self,
    ) -> tuple[tuple[tuple[object, object, object], object], ...]:
        return self.registry_items + tuple(self.registry_entities_set)

    def type_by_name(self, type_name: str) -> object | None:
        return self.type_names_to_return.get(type_name)

    def set_registry_entity(
        self,
        key: tuple[object, object, object],
        value: object,
    ) -> None:
        self.registry_entities_set.append((key, value))

    def task_values_snapshot(self) -> dict[str, object]:
        return {
            str(key[2]): value
            for key, value in self.registry_items
            if key[0] == "task"
        }

    def action_values_snapshot(self) -> dict[str, object]:
        return {
            str(key[2]): value
            for key, value in self.registry_items
            if key[0] == "action"
        }

    def value_values_snapshot(self) -> dict[str, object]:
        return {
            str(key[2]): value
            for key, value in self.registry_items
            if key[0] == "value"
        }

    def configs_snapshot(self) -> list[tuple[str, object]]:
        return list(self.configs_to_return)


def test_workspace_loader_collects_all_phase_two_failures(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")
    bad_a = project / "mlody" / "teams" / "lexica" / "bad_a.mlody"
    bad_b = project / "mlody" / "teams" / "lexica" / "bad_b.mlody"
    good = project / "mlody" / "teams" / "lexica" / "good.mlody"
    fs.create_file(str(bad_a), contents="bad a")
    fs.create_file(str(bad_b), contents="bad b")
    fs.create_file(str(good), contents="good")

    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        },
        eval_failures={
            bad_a: SyntaxError("bad a"),
            bad_b: ValueError("bad b"),
        },
    )
    converted: list[str] = []
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: converted.append("converted"),
        resolve_value_sources=lambda: None,
    )

    with pytest.raises(WorkspaceLoadError) as exc_info:
        loader.load()

    failures = exc_info.value.failures
    assert {path.name for path, _exc in failures} == {"bad_a.mlody", "bad_b.mlody"}
    assert any(path.name == "good.mlody" for path in registry.eval_calls)
    assert converted == []
    assert registry.resolved is False


def test_workspace_loader_injects_extra_and_lazy_roots(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")

    root_infos: dict[str, RootInfo] = {}
    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        }
    )
    converted: list[str] = []
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos=root_infos,
        registry=registry,  # type: ignore[arg-type]
        extra_roots={"workspace": "//sandbox"},
        lazy_roots={"mlody": "//mlody"},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: converted.append("converted"),
        resolve_value_sources=lambda: None,
    )

    loader.load()

    assert root_infos["workspace"] == RootInfo(
        name="workspace",
        path="//sandbox",
        description="injected",
    )
    assert ("workspace", "//sandbox", "injected") in registry.placeholders
    assert ("mlody", "//mlody", "injected") in registry.placeholders
    assert converted == ["converted"]
    assert registry.resolved is True


def test_workspace_loader_propagates_stage_value_injection(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "common" / "hash.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "common" / "mm.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "common" / "render.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "common" / "config.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "roots.mlody"), contents="")

    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        }
    )
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: True,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
    )

    loader.load()

    render_path = project / "mlody" / "common" / "render.mlody"
    hash_path = project / "mlody" / "common" / "hash.mlody"
    propagated = {
        path: names for path, names in registry.propagated_injections
    }
    assert "hash" in propagated[hash_path]
    assert "stage_value" in propagated[render_path]


def test_workspace_loader_validates_contextual_values_after_port_conversion(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")

    value = Struct(
        kind="value",
        name="artifact",
        group="bundle",
        _context_attr_policies={"group": ("task.outputs",)},
    )
    task = Struct(
        kind="task",
        name="train",
        inputs=[value],
        outputs=[],
        config=[],
        action=Struct(kind="action", name="act", inputs=[], outputs=[], config=[]),
    )

    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        },
        registry_items=(
            (("value", "pkg", "artifact"), value),
            (("task", "pkg", "train"), task),
        ),
    )
    converted: list[str] = []
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: converted.append("converted"),
        resolve_value_sources=lambda: None,
    )

    with pytest.raises(ContextRestrictedValueValidationError):
        loader.load()


def test_workspace_loader_injects_synthetic_mav_user(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")

    user_type = Struct(name="mlody-user")
    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        },
        type_names_to_return={"mlody-user": user_type},
    )
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: True,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
    )

    loader.load()

    assert len(registry.registry_entities_set) == 1
    key, value = registry.registry_entities_set[0]
    assert key == ("user", "", "mav")
    assert value.kind == "user"
    assert value.name == "mav"
    assert value.description == "Maurizio Vitale"
    assert value.groups == ["admin"]
    assert value.avatar == "assets/images/avatars/avatars-4-2.png"
    assert value._entity_type is user_type


def test_workspace_loader_keeps_explicit_mav_user(
    fs: FakeFilesystem,
) -> None:
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")

    registry = _FakeRegistry(
        root_infos_to_return={
            "lexica": RootInfo(
                name="lexica",
                path="//mlody/teams/lexica",
                description="team root",
            )
        },
        registry_items=(
            (
                ("user", "", "mav"),
                Struct(
                    kind="user",
                    name="mav",
                    description="Existing User",
                    groups=["ops"],
                ),
            ),
        ),
    )
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: True,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
    )

    loader.load()

    assert registry.registry_entities_set == []
    assert registry.resolved is True


# ---------------------------------------------------------------------------
# Phase 4: Config application tests (tasks 10.1, 10.2, 10.3)
# ---------------------------------------------------------------------------


def test_workspace_loader_config_application_noop_when_no_configs(
    fs: FakeFilesystem,
) -> None:
    """load() completes and calls convert_ports_to_structs even with no configs.

    Ref: Scenario 'No configs — zero-cost no-op'.
    Config application is in configure_workspace (resolver.py), not the loader.
    """
    project = Path("/workspace")
    fs.create_dir(str(project / "mlody" / "teams" / "lexica"))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")

    registry = _FakeRegistry(
        root_infos_to_return={},
        configs_to_return=[],
    )
    converted: list[str] = []
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: converted.append("converted"),
        resolve_value_sources=lambda: None,
    )

    loader.load()
    assert converted == ["converted"]


# ---------------------------------------------------------------------------
# Wave 1f — Reporter + WorkspaceLoader verbose output (tasks 5.12, 5.13)
# ---------------------------------------------------------------------------


def _make_minimal_loader(
    project: Path,
    reporter: "Reporter | None" = None,
    root_infos_to_return: dict[str, RootInfo] | None = None,
) -> WorkspaceLoader:
    """Build a WorkspaceLoader with a fake registry and no roots for quick tests."""
    registry = _FakeRegistry(root_infos_to_return=root_infos_to_return or {})
    return WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
        reporter=reporter,
    )


def test_reporter_default_emits_nothing(fs: FakeFilesystem) -> None:
    """Scenario: verbose=False (default) emits no output from WorkspaceLoader."""
    project = Path("/workspace")
    fs.create_dir(str(project))

    calls: list[str] = []
    reporter = Reporter(print_fn=lambda msg, *a, **kw: calls.append(str(msg)))
    loader = _make_minimal_loader(project, reporter=reporter)
    loader.load()

    assert calls == [], f"Expected no output but got: {calls}"


def test_reporter_verbose_emits_phase_lines(fs: FakeFilesystem) -> None:
    """Scenario: verbose=True emits all phase 1/2 lines from WorkspaceLoader.

    Tests patterns: phase 1 start, phase 1 end, phase 2 start, phase 2 end.
    """
    project = Path("/workspace")
    fs.create_dir(str(project))
    fs.create_file(str(project / "mlody" / "common" / "types.mlody"), contents="")
    fs.create_file(str(project / "mlody" / "teams" / "lexica" / "model.mlody"), contents="")

    root_infos = {
        "lexica": RootInfo(name="lexica", path="mlody/teams/lexica", description="team")
    }
    calls: list[str] = []
    reporter = Reporter(
        print_fn=lambda msg, *a, **kw: calls.append(str(msg)),
        verbose=True,
    )
    registry = _FakeRegistry(root_infos_to_return=root_infos)
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos={},
        registry=registry,  # type: ignore[arg-type]
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _path: False,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
        reporter=reporter,
    )
    loader.load()

    output = "\n".join(calls)
    assert re.search(r"\[mlody\] phase 1: root discovery started", output)
    assert re.search(r"\[mlody\] phase 1: root discovery done \(\d+ roots\) \[\d+\.\d{2}s\]", output)
    assert re.search(r"\[mlody\] phase 2: full evaluation started \(\d+ roots\)", output)
    assert re.search(r"\[mlody\]   loading .+model\.mlody", output)
    assert re.search(r"\[mlody\] phase 2: done \(\d+ files loaded, \d+ errors\) \[\d+\.\d{2}s\]", output)


def test_workspace_loader_load_verbose_kwarg_raises_type_error(
    fs: FakeFilesystem,
) -> None:
    """Scenario: WorkspaceLoader.load(verbose=True) raises TypeError after refactor."""
    project = Path("/workspace")
    fs.create_dir(str(project))
    loader = _make_minimal_loader(project)

    with pytest.raises(TypeError):
        loader.load(verbose=True)  # type: ignore[call-arg]


def test_reporter_verbose_elapsed_non_negative(fs: FakeFilesystem) -> None:
    """Scenario: all elapsed time values parsed from verbose output are >= 0."""
    project = Path("/workspace")
    fs.create_dir(str(project))

    calls: list[str] = []
    reporter = Reporter(
        print_fn=lambda msg, *a, **kw: calls.append(str(msg)),
        verbose=True,
    )
    loader = _make_minimal_loader(project, reporter=reporter)
    loader.load()

    output = "\n".join(calls)
    elapsed_values = re.findall(r"\[(\d+\.\d+)s\]", output)
    assert len(elapsed_values) >= 2, f"Expected elapsed time in output, got: {output}"
    for val in elapsed_values:
        assert float(val) >= 0.0, f"Elapsed time {val} is negative"


def test_cache_hit_and_miss_lines_emitted_on_consecutive_calls(
    tmp_path: Path,
) -> None:
    """Scenario: cache miss on first call, cache hit on second call, both logged.

    Tests patterns: cache key, cache miss, cache hit.
    """
    calls: list[str] = []

    def capture(msg: object, *a: object, **kw: object) -> None:
        calls.append(str(msg))

    reporter = Reporter(print_fn=capture, verbose=True)
    req = _make_workspace_request(mode="cwd", monorepo_root=tmp_path)
    evict_baseline_workspace(req)

    raw_workspace = MagicMock()
    baseline_workspace = MagicMock()

    with (
        patch("mlody.resolver.resolver.Workspace", return_value=raw_workspace),
        patch(
            "mlody.resolver.resolver.build_baseline_workspace",
            return_value=baseline_workspace,
        ),
    ):
        get_or_build_baseline_workspace(req, reporter)
        get_or_build_baseline_workspace(req, reporter)

    output = "\n".join(calls)
    assert re.search(r"\[mlody\] cache key:", output), f"Missing cache key line in: {output}"
    assert re.search(r"\[mlody\] cache miss", output), f"Missing cache miss line in: {output}"
    assert re.search(r"\[mlody\] cache hit for", output), f"Missing cache hit line in: {output}"
    evict_baseline_workspace(req)
