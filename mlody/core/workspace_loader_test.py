"""Focused tests for the workspace loading orchestration service."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pyfakefs.fake_filesystem import FakeFilesystem
import pytest

from mlody.core.workspace_loader import WorkspaceLoader
from mlody.core.workspace_models import RootInfo, WorkspaceLoadError


@dataclass
class _FakeRegistry:
    root_infos_to_return: dict[str, RootInfo]
    eval_failures: dict[Path, Exception] = field(default_factory=dict)
    loaded_files: set[Path] = field(default_factory=set)
    eval_calls: list[Path] = field(default_factory=list)
    placeholders: list[tuple[str, str, str]] = field(default_factory=list)
    resolved: bool = False

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
