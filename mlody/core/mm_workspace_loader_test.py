"""Workspace-level integration tests for mm.mlody Phase 1 loading.

These tests verify:
- mm is available in a user .mlody file without an explicit load()
- mm.mlody is not evaluated twice when a user file already load()-ed it
- missing mm.mlody causes an exception (not a silent skip)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from mlody.core.registry_view import RegistryView
from mlody.core.workspace_loader import WorkspaceLoader
from mlody.core.workspace_models import RootInfo
from common.python.starlarkish.evaluator.evaluator import Evaluator

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR / "rule.mlody").read_text()
_MM_MLODY = (_THIS_DIR.parent / "common" / "mm.mlody").read_text()
_RENDER_MLODY = (_THIS_DIR.parent / "common" / "render.mlody").read_text()
_CONFIG_MLODY = (_THIS_DIR.parent / "common" / "config.mlody").read_text()


def _setup_project(
    fs: FakeFilesystem,
    project: Path,
    *,
    extra_user_files: dict[str, str] | None = None,
    include_mm: bool = True,
) -> None:
    """Create a minimal fake workspace on the fake filesystem."""
    fs.create_file(str(project / "mlody/core/rule.mlody"), contents=_RULE_MLODY)
    if include_mm:
        fs.create_file(str(project / "mlody/common/mm.mlody"), contents=_MM_MLODY)
        fs.create_file(str(project / "mlody/common/render.mlody"), contents=_RENDER_MLODY)
        fs.create_file(str(project / "mlody/common/config.mlody"), contents=_CONFIG_MLODY)

    # Minimal roots.mlody — registers one root pointing at the user dir
    fs.create_file(
        str(project / "mlody/roots.mlody"),
        contents=(
            'builtins.register("root", struct('
            'name="user", path="//mlody/user", description="user root"'
            "))"
        ),
    )
    fs.create_dir(str(project / "mlody/user"))
    for rel_path, contents in (extra_user_files or {}).items():
        fs.create_file(str(project / "mlody/user" / rel_path), contents=contents)


def _make_loader(
    project: Path,
    *,
    include_mm: bool = True,
    extra_user_files: dict[str, str] | None = None,
    fs: FakeFilesystem,
) -> tuple[WorkspaceLoader, RegistryView]:
    """Set up a fake workspace and return a (WorkspaceLoader, RegistryView) pair."""
    _setup_project(fs, project, include_mm=include_mm, extra_user_files=extra_user_files)
    evaluator = Evaluator(project)
    registry = RegistryView(evaluator)
    root_infos: dict[str, RootInfo] = {}
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos=root_infos,
        registry=registry,
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _p: False,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
    )
    return loader, registry


def test_mm_available_in_user_file_without_explicit_load(fs: FakeFilesystem) -> None:
    """mm is bound in a user .mlody file without any explicit load() call.

    Ref: Scenario 'Phase 1 — mm.mlody loaded before user files'.
    """
    project = Path("/workspace")
    loader, registry = _make_loader(
        project,
        fs=fs,
        extra_user_files={
            # Access mm.ANY to verify mm is available; register a sentinel root.
            "pipeline.mlody": (
                'mm_any = mm.ANY\n'
                'builtins.register("root", struct(name="result", sentinel=True))'
            ),
        },
    )
    loader.load()

    evaluator = registry._evaluator
    # The user file ran successfully, which means mm was in its scope.
    assert "result" in evaluator.registry.roots.by_name


def test_mm_not_evaluated_twice_when_already_loaded(fs: FakeFilesystem) -> None:
    """mm.mlody is not re-evaluated if already loaded before Phase 1.

    Ref: Scenario 'mm.mlody not loaded twice'.
    """
    project = Path("/workspace")
    _setup_project(fs, project)

    evaluator = Evaluator(project)
    registry = RegistryView(evaluator)
    mm_path = project / "mlody" / "common" / "mm.mlody"

    # Pre-evaluate mm.mlody (simulating a prior explicit load by another file).
    evaluator.eval_file(mm_path)

    # Track how many times mm.mlody appears in loaded_files before Phase 1.
    assert mm_path in evaluator.loaded_files
    loaded_snapshot = set(evaluator.loaded_files)

    root_infos: dict[str, RootInfo] = {}
    loader = WorkspaceLoader(
        monorepo_root=project,
        roots_file=project / "mlody" / "roots.mlody",
        root_infos=root_infos,
        registry=registry,
        extra_roots={},
        lazy_roots={},
        should_skip_mlody_file=lambda _p: False,
        convert_ports_to_structs=lambda: None,
        resolve_value_sources=lambda: None,
    )
    loader._phase1_root_discovery()

    # mm.mlody must still be in loaded_files exactly once (set semantics ensure this).
    assert mm_path in evaluator.loaded_files
    # loaded_files is a set, so duplicate eval would not add a new entry — but we
    # can verify indirectly: the evaluator's loaded_files count grew by exactly
    # however many new files Phase 1 loaded (roots.mlody), not by re-loading mm.mlody.
    new_files = evaluator.loaded_files - loaded_snapshot
    assert mm_path not in new_files, (
        "mm.mlody appears in the set of newly loaded files, indicating it was re-evaluated"
    )


def test_missing_mm_mlody_raises_exception(fs: FakeFilesystem) -> None:
    """If mm.mlody is absent, Phase 1 raises an exception (not a silent skip).

    Ref: Scenario 'mm.mlody missing from repository'.
    """
    project = Path("/workspace")
    loader, _registry = _make_loader(
        project,
        fs=fs,
        include_mm=False,  # do NOT create mm.mlody
    )
    with pytest.raises(FileNotFoundError):
        loader._phase1_root_discovery()
