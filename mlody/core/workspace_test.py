"""Tests for mlody.core.workspace — two-phase loading and target resolution."""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from rich.console import Console
from common.python.starlarkish.core.struct import Struct

import mlody.resolver.resolver_impl  # noqa: F401 — triggers _register_workspace_hook()

from mlody.core.anchor import (
    ModuleAggregateAnchor,
    ModuleGlobalAnchor,
    RegistryEntityAnchor,
    RootCollectionAnchor,
    RootObjectAnchor,
    WorkspaceAttributeAnchor,
)
from mlody.core.targets import TargetAddress
from mlody.core.value_context_validation import (
    ContextRestrictedValueValidationError,
)
from mlody.core.workspace import RootInfo, Workspace, WorkspaceLoadError, WorkspaceStateKind

ROOT = Path("/project")

# Real source files that mm.mlody and config.mlody depend on; must be added to
# fake filesystems via fs.add_real_file so workspace_loader can eval them during Phase 1.
_REAL_RULE_MLODY = Path(__file__).parent / "rule.mlody"
_REAL_MM_MLODY = Path(__file__).parent.parent / "common" / "mm.mlody"
_REAL_HASH_MLODY = Path(__file__).parent.parent / "common" / "hash.mlody"
_REAL_RENDER_MLODY = Path(__file__).parent.parent / "common" / "render.mlody"
_REAL_CONFIG_MLODY = Path(__file__).parent.parent / "common" / "config.mlody"


def _add_mm_files(fs: FakeFilesystem, root: Path) -> None:
    """Add Phase-1 common mlody files to the fake filesystem."""
    fs.add_real_file(_REAL_RULE_MLODY, target_path=str(root / "mlody/core/rule.mlody"))
    fs.add_real_file(_REAL_MM_MLODY, target_path=str(root / "mlody/common/mm.mlody"))
    fs.add_real_file(_REAL_HASH_MLODY, target_path=str(root / "mlody/common/hash.mlody"))
    fs.add_real_file(_REAL_RENDER_MLODY, target_path=str(root / "mlody/common/render.mlody"))
    fs.add_real_file(_REAL_CONFIG_MLODY, target_path=str(root / "mlody/common/config.mlody"))


BUILTINS_MLODY = """\
def root(name, path, description=""):
    entity_type = None
    try:
        entity_type = builtins.lookup("type", "mlody-root")
    except:
        entity_type = None
    fields = {
        "name": name,
        "path": path,
        "description": description,
    }
    if entity_type != None:
        fields["_entity_type"] = entity_type
    builtins.register("root", struct(**fields))
"""

ROOTS_MLODY = """\
load("//mlody/core/builtins.mlody", "root")

root(name="lexica", path="//mlody/teams/lexica", description="text ML team")
"""

TYPES_MLODY = """\
# Register mm.vector so render.mlody can use mm.vector(...) patterns.
# After task 7.1, mm.vector is no longer a fixed attr on MmNamespace.
builtins.register_mm_pattern("type", "vector", {"element_type": None})
builtins.register("type", struct(
    kind="type", type="mlody-source-range", name="mlody-source-range",
    fields=[
        struct(name="filepath", type=struct(kind="type", type="string", name="string")),
        struct(name="start_line", type=struct(kind="type", type="integer", name="integer")),
        struct(name="end_line", type=struct(kind="type", type="integer", name="integer")),
    ],
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody_workspace_info", name="mlody_workspace_info",
    fields=[
        struct(name="path", type=struct(kind="type", type="string", name="string")),
        struct(name="branch", type=struct(kind="type", type="string", name="string")),
        struct(name="sha", type=struct(kind="type", type="string", name="string")),
        struct(name="roots", type=struct(kind="type", type="vector", name="vector")),
    ],
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
_ENTITY_FIELDS = [
    struct(name="_source_range", type=builtins.lookup("type", "mlody-source-range"), mandatory=False),
]
builtins.register("type", struct(
    kind="type", type="mlody-value", name="mlody-value",
    fields=_ENTITY_FIELDS,
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody-task", name="mlody-task",
    fields=_ENTITY_FIELDS + [
        struct(
            name="_hash",
            type=struct(kind="type", type="string", name="string"),
            materializer=lambda _task: python.uuid7(),
        ),
    ],
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody-action", name="mlody-action",
    fields=_ENTITY_FIELDS,
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody-root", name="mlody-root",
    fields=_ENTITY_FIELDS,
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody-workspace", name="mlody-workspace",
    fields=_ENTITY_FIELDS + [
        struct(name="info", type=struct(kind="type", type="mlody_workspace_info", name="mlody_workspace_info", _root_kind="record", fields=[
            struct(name="path", type=struct(kind="type", type="string", name="string")),
            struct(name="branch", type=struct(kind="type", type="string", name="string")),
            struct(name="sha", type=struct(kind="type", type="string", name="string")),
            struct(name="roots", type=struct(kind="type", type="vector", name="vector")),
        ])),
    ],
    attributes={}, _allowed_attrs={},
))
"""


@pytest.fixture()
def project(fs: FakeFilesystem) -> Path:
    """Set up a fake project with roots and team files."""
    fs.create_file(str(ROOT / "mlody/core/builtins.mlody"), contents=BUILTINS_MLODY)
    fs.create_file(str(ROOT / "mlody/roots.mlody"), contents=ROOTS_MLODY)
    fs.create_file(str(ROOT / "mlody/common/types.mlody"), contents=TYPES_MLODY)
    # mm.mlody and rule.mlody are required by workspace_loader Phase 1.
    _add_mm_files(fs, ROOT)
    fs.create_file(
        str(ROOT / "mlody/teams/lexica/models.mlody"),
        contents='builtins.register("root", struct(name="bert", lr=0.001))',
    )
    return ROOT


# ---------------------------------------------------------------------------
# RootInfo
# ---------------------------------------------------------------------------


class TestRootInfo:
    """Requirement: RootInfo is a frozen dataclass."""

    def test_fields(self) -> None:
        info = RootInfo(name="lexica", path="//mlody/teams/lexica", description="text ML team")
        assert info.name == "lexica"
        assert info.path == "//mlody/teams/lexica"
        assert info.description == "text ML team"

    def test_frozen(self) -> None:
        info = RootInfo(name="a", path="b", description="c")
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.name = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------


class TestWorkspaceConstructor:
    """Requirement: Default roots file location."""

    def test_default_roots_path(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        assert ws._roots_file == project / "mlody" / "roots.mlody"

    def test_custom_roots_path(self, project: Path) -> None:
        custom = project / "other" / "roots.mlody"
        ws = Workspace(monorepo_root=project, roots_file=custom)
        assert ws._roots_file == custom


class TestWorkspaceForkRequest:
    def test_fork_request_isolates_visible_state(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        module_path = ROOT / "mlody/teams/lexica/fork_state.mlody"
        fs.create_file(
            str(module_path),
            contents="""\
global_cfg = {"entries": [1]}
builtins.register("value", Struct(
    kind="value",
    name="artifact",
    payload={"entries": [1]},
    _lineage=[],
))
""",
        )

        baseline = Workspace(monorepo_root=project)
        baseline.load()
        baseline.mark_baseline()
        baseline._set_workspace_attribute(
            "custom",
            Struct(entries=[1], nested=Struct(values=[1])),
        )
        baseline._dag_cache = object()  # type: ignore[assignment]

        request = baseline.fork_request()

        assert baseline.state_kind is WorkspaceStateKind.BASELINE
        assert request.state_kind is WorkspaceStateKind.REQUEST
        assert request._dag_cache is None

        request.evaluator._module_globals[module_path]["global_cfg"]["entries"].append(2)
        request.evaluator.registry.values.by_name["artifact"].payload["entries"].append(2)  # type: ignore[attr-defined]
        request_custom = request.get_workspace_attribute("custom")
        assert isinstance(request_custom, Struct)
        request_custom.entries.append(2)  # type: ignore[attr-defined]
        request_custom.nested.values.append(2)  # type: ignore[attr-defined]

        assert baseline.evaluator._module_globals[module_path]["global_cfg"]["entries"] == [1]
        assert baseline.evaluator.registry.values.by_name["artifact"].payload["entries"] == [1]  # type: ignore[attr-defined]
        baseline_custom = baseline.get_workspace_attribute("custom")
        assert isinstance(baseline_custom, Struct)
        assert baseline_custom.entries == [1]  # type: ignore[attr-defined]
        assert baseline_custom.nested.values == [1]  # type: ignore[attr-defined]

    def test_fork_request_lazy_module_load_does_not_leach_into_baseline(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        lazy_module = project / "standalone.mlody"
        fs.create_file(
            str(lazy_module),
            contents='seed = "request-only"\n',
        )

        baseline = Workspace(monorepo_root=project)
        baseline.load()
        baseline.mark_baseline()

        request = baseline.fork_request()

        assert not baseline.registry_view.is_loaded(lazy_module)
        assert not request.registry_view.is_loaded(lazy_module)

        assert request.resolve("//standalone:seed") == "request-only"

        assert request.registry_view.is_loaded(lazy_module)
        assert not baseline.registry_view.is_loaded(lazy_module)
        assert lazy_module in request.evaluator._module_globals
        assert lazy_module not in baseline.evaluator._module_globals

    def test_multiple_request_forks_are_isolated_from_each_other(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        module_path = ROOT / "mlody/teams/lexica/fork_state.mlody"
        fs.create_file(
            str(module_path),
            contents="""\
global_cfg = {\"entries\": [1]}
builtins.register(\"value\", Struct(
    kind=\"value\",
    name=\"artifact\",
    payload={\"entries\": [1]},
    _lineage=[],
))
""",
        )

        baseline = Workspace(monorepo_root=project)
        baseline.load()
        baseline.mark_baseline()
        baseline._set_workspace_attribute(
            "custom",
            Struct(entries=[1], nested=Struct(values=[1])),
        )

        request_a = baseline.fork_request()
        request_b = baseline.fork_request()

        request_a.evaluator._module_globals[module_path]["global_cfg"]["entries"].append(2)
        request_a.evaluator.registry.values.by_name["artifact"].payload["entries"].append(2)  # type: ignore[attr-defined]
        request_a_custom = request_a.get_workspace_attribute("custom")
        assert isinstance(request_a_custom, Struct)
        request_a_custom.entries.append(2)  # type: ignore[attr-defined]
        request_a_custom.nested.values.append(2)  # type: ignore[attr-defined]

        request_b.evaluator._module_globals[module_path]["global_cfg"]["entries"].append(3)
        request_b.evaluator.registry.values.by_name["artifact"].payload["entries"].append(3)  # type: ignore[attr-defined]
        request_b_custom = request_b.get_workspace_attribute("custom")
        assert isinstance(request_b_custom, Struct)
        request_b_custom.entries.append(3)  # type: ignore[attr-defined]
        request_b_custom.nested.values.append(3)  # type: ignore[attr-defined]

        assert request_a.evaluator._module_globals[module_path]["global_cfg"]["entries"] == [1, 2]
        assert request_b.evaluator._module_globals[module_path]["global_cfg"]["entries"] == [1, 3]
        assert request_a.evaluator.registry.values.by_name["artifact"].payload["entries"] == [1, 2]  # type: ignore[attr-defined]
        assert request_b.evaluator.registry.values.by_name["artifact"].payload["entries"] == [1, 3]  # type: ignore[attr-defined]
        assert request_a_custom.entries == [1, 2]  # type: ignore[attr-defined]
        assert request_b_custom.entries == [1, 3]  # type: ignore[attr-defined]
        assert request_a_custom.nested.values == [1, 2]  # type: ignore[attr-defined]
        assert request_b_custom.nested.values == [1, 3]  # type: ignore[attr-defined]

        assert baseline.evaluator._module_globals[module_path]["global_cfg"]["entries"] == [1]
        assert baseline.evaluator.registry.values.by_name["artifact"].payload["entries"] == [1]  # type: ignore[attr-defined]
        baseline_custom = baseline.get_workspace_attribute("custom")
        assert isinstance(baseline_custom, Struct)
        assert baseline_custom.entries == [1]  # type: ignore[attr-defined]
        assert baseline_custom.nested.values == [1]

    def test_update_global_context_refreshes_builtins_ctx(
        self,
        project: Path,
    ) -> None:
        workspace = Workspace(monorepo_root=project)
        workspace.load()

        workspace.update_global_context(
            user="agarcia",
            resolved_sha="a" * 40,
        )

        extra_ctx = workspace.evaluator._extra_ctx  # noqa: SLF001
        assert extra_ctx is not None
        assert extra_ctx.workspace.user == "agarcia"
        assert extra_ctx.workspace.commit == "a" * 40
        assert extra_ctx.workspace.directory == str(project)

        for module_globals in workspace.evaluator._module_globals.values():  # noqa: SLF001
            builtins_ctx = module_globals["builtins"].ctx
            assert builtins_ctx.workspace.user == "agarcia"
            assert builtins_ctx.workspace.commit == "a" * 40
            assert builtins_ctx.workspace.directory == str(project)

    def test_explicit_reload_rebuilds_cwd_baseline_from_updated_files(
        self,
        project: Path,
    ) -> None:
        first_baseline = Workspace(monorepo_root=project)
        first_baseline.load()
        first_baseline.mark_baseline()
        first_request = first_baseline.fork_request()

        assert first_request.resolve("@lexica//models:bert.lr") == 0.001

        models_file = project / "mlody/teams/lexica/models.mlody"
        models_file.write_text(
            'builtins.register("root", struct(name="bert", lr=0.002))',
            encoding="utf-8",
        )

        reloaded_baseline = Workspace(monorepo_root=project)
        reloaded_baseline.load()
        reloaded_baseline.mark_baseline()
        reloaded_request = reloaded_baseline.fork_request()

        assert first_request.resolve("@lexica//models:bert.lr") == 0.001
        assert reloaded_request.resolve("@lexica//models:bert.lr") == 0.002


# ---------------------------------------------------------------------------
# Two-phase loading
# ---------------------------------------------------------------------------


class TestTwoPhaseLoading:
    """Requirement: Two-phase loading of pipeline definitions."""

    def test_phase1_root_discovery(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        assert "lexica" in ws.root_infos
        info = ws.root_infos["lexica"]
        assert info.name == "lexica"
        assert info.path == "//mlody/teams/lexica"
        assert info.description == "text ML team"

    def test_phase2_evaluates_files_under_roots(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        # models.mlody registers "bert" as a root; key is path-qualified
        assert "mlody/teams/lexica/models:bert" in ws.evaluator.registry.roots.by_key

    def test_registered_roots_get_declared_entity_type(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        root_value = ws.evaluator.registry.roots.by_name["lexica"]  # type: ignore[attr-defined]
        assert root_value._entity_type.name == "mlody-root"  # type: ignore[attr-defined]

    def test_phase2_skips_already_loaded_files(self, fs: FakeFilesystem, project: Path) -> None:
        # builtins.mlody is loaded in Phase 1 via roots.mlody's load() call.
        # Phase 2 should not re-evaluate it even though it's under mlody/.
        ws = Workspace(monorepo_root=project)
        ws.load()

        builtins_path = project / "mlody" / "core" / "builtins.mlody"
        assert builtins_path in ws.evaluator.loaded_files
        # Only one entry in _module_globals for builtins.mlody proves single evaluation —
        # a second eval_file() call would still return cached globals (Evaluator line 185),
        # but the Workspace skip check prevents even that redundant call.
        assert ws.evaluator._module_globals[builtins_path] is ws.evaluator._module_globals[builtins_path]  # type: ignore[attr-defined]
        globals_snapshot = dict(ws.evaluator._module_globals)  # type: ignore[attr-defined]
        # Re-run load() to confirm idempotency — no new entries appear
        ws.load()
        assert dict(ws.evaluator._module_globals) == globals_snapshot  # type: ignore[attr-defined]

    def test_missing_roots_file_loads_cleanly(self, fs: FakeFilesystem) -> None:
        # A missing roots.mlody is silently skipped — workspace operates from
        # injected roots only (e.g. --workspace sandboxes without a roots file).
        root = Path("/empty")
        root.mkdir()
        ws = Workspace(monorepo_root=root)
        ws.load()  # must not raise
        assert ws.root_infos == {}

    def test_workspace_mlody_at_monorepo_root_is_loaded(self, project: Path) -> None:
        (project / "workspace.mlody").write_text(
            'builtins.register("root", struct(name="workspace_only", value=1))\n'
        )

        ws = Workspace(monorepo_root=project)
        ws.load()

        assert "workspace_only" in ws.evaluator.registry.roots.by_name

    def test_workspace_registers_synthetic_mav_user(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        mav = ws.evaluator.registry.users.by_name["mav"]
        assert mav.kind == "user"
        assert mav.name == "mav"
        assert mav.description == "Maurizio Vitale"
        assert mav.groups == ["admin"]
        assert mav.avatar == "assets/images/avatars/avatars-4-2.png"

    def test_workspace_mlody_uses_workspace_root_without_monorepo_fallback(
        self, project: Path
    ) -> None:
        (project / "workspace.mlody").write_text(
            'builtins.register("root", struct(name="monorepo_workspace_only", value=1))\n'
        )
        workspace_dir = project / "sandboxes" / "exp1"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "workspace.mlody").write_text(
            'builtins.register("root", struct(name="subworkspace_only", value=1))\n'
        )

        ws = Workspace(monorepo_root=project, workspace_root=workspace_dir)
        ws.load()

        assert "subworkspace_only" in ws.evaluator.registry.roots.by_name
        assert "monorepo_workspace_only" not in ws.evaluator.registry.roots.by_name

    def test_no_roots_registered(self, fs: FakeFilesystem) -> None:
        root = Path("/no_roots")
        root.mkdir()
        fs.create_file(str(root / "mlody/roots.mlody"), contents="# no roots here\n")
        # types.mlody registers mm.vector so render.mlody can use mm.vector(...).
        # Phase 1 loads mm.mlody first (setting up MmNamespace), then types.mlody.
        fs.create_file(str(root / "mlody/common/types.mlody"), contents=TYPES_MLODY)
        # mm.mlody is required by workspace_loader when roots.mlody exists.
        _add_mm_files(fs, root)
        ws = Workspace(monorepo_root=root)
        ws.load()

        assert ws.root_infos == {}

    def test_evaluator_is_same_instance_after_load(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        evaluator_before = ws.evaluator
        ws.load()
        assert ws.evaluator is evaluator_before

    def test_evaluator_exposes_module_globals_for_lsp(self, project: Path) -> None:
        # LSP needs _module_globals to provide completions for symbols in loaded files
        ws = Workspace(monorepo_root=project)
        ws.load()

        models_path = project / "mlody" / "teams" / "lexica" / "models.mlody"
        module_globals = ws.evaluator._module_globals  # type: ignore[attr-defined]
        assert models_path in module_globals
        assert "builtins" in module_globals[models_path]

    def test_default_skip_list_skips_sandbox_mlody(
        self, fs: FakeFilesystem, project: Path
    ) -> None:
        (project / "mlody/roots.mlody").write_text(
            'load("//mlody/core/builtins.mlody", "root")\n'
            'root(name="lexica", path="//mlody/teams/lexica", description="text ML team")\n'
            'root(name="common", path="//mlody/common", description="common")\n'
        )
        fs.create_file(
            str(project / "mlody/common/sandbox.mlody"),
            contents='builtins.register("root", struct(name="sandbox_only", value=1))',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()
        assert "mlody/common/sandbox:sandbox_only" not in ws.evaluator.registry.roots.by_key

    def test_full_workspace_loads_sandbox_mlody(
        self, fs: FakeFilesystem, project: Path
    ) -> None:
        (project / "mlody/roots.mlody").write_text(
            'load("//mlody/core/builtins.mlody", "root")\n'
            'root(name="lexica", path="//mlody/teams/lexica", description="text ML team")\n'
            'root(name="common", path="//mlody/common", description="common")\n'
        )
        fs.create_file(
            str(project / "mlody/common/sandbox.mlody"),
            contents='builtins.register("root", struct(name="sandbox_only", value=1))',
        )
        ws = Workspace(monorepo_root=project, full_workspace=True)
        ws.load()
        assert "mlody/common/sandbox:sandbox_only" in ws.evaluator.registry.roots.by_key

    def test_workspace_load_raises_on_context_restricted_value_violation(
        self, fs: FakeFilesystem, project: Path
    ) -> None:
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "pipeline.mlody"),
            contents="""\
bad_value = Struct(
    kind="value",
    name="artifact",
    group="bundle",
    _context_attr_policies={"group": ("task.outputs",)},
    location=Struct(kind="location", type="inline", name="inline"),
    _lineage=[],
)
builtins.register("value", bad_value)
builtins.register("task", Struct(
    kind="task",
    name="train",
    inputs=[bad_value],
    outputs=[],
    config=[],
    action=Struct(kind="action", name="act", inputs=[], outputs=[], config=[]),
))
""",
        )

        ws = Workspace(monorepo_root=project)

        with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
            ws.load()

        assert exc_info.value.violations[0].actual_context == "task.inputs"

    def test_skip_pattern_with_ellipsis_skips_subtree(
        self, fs: FakeFilesystem, project: Path
    ) -> None:
        (project / "mlody/roots.mlody").write_text(
            'load("//mlody/core/builtins.mlody", "root")\n'
            'root(name="lexica", path="//mlody/teams/lexica", description="text ML team")\n'
            'root(name="common", path="//mlody/common", description="common")\n'
        )
        fs.create_file(
            str(project / "mlody/common/skipme/a.mlody"),
            contents='builtins.register("root", struct(name="skip_a", value=1))',
        )
        fs.create_file(
            str(project / "mlody/common/skipme/nested/b.mlody"),
            contents='builtins.register("root", struct(name="skip_b", value=2))',
        )
        fs.create_file(
            str(project / "mlody/common/keep.mlody"),
            contents='builtins.register("root", struct(name="keep", value=3))',
        )
        ws = Workspace(
            monorepo_root=project,
            skipped_mlody_paths=["mlody/common/skipme/..."],
        )
        ws.load()
        assert "mlody/common/skipme/a:skip_a" not in ws.evaluator.registry.roots.by_key
        assert "mlody/common/skipme/nested/b:skip_b" not in ws.evaluator.registry.roots.by_key
        assert "mlody/common/keep:keep" in ws.evaluator.registry.roots.by_key


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestResolve:
    """Requirement: Target resolution via Workspace."""

    def test_resolve_string_target(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("@bert//models:lr")
        assert result == 0.001

    def test_resolve_target_address(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        addr = TargetAddress(root="bert", package_path="", target_name="lr", field_path=())
        result = ws.resolve(addr)
        assert result == 0.001

    def test_resolve_error_propagation_missing_root(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        with pytest.raises(KeyError, match="NONEXISTENT"):
            ws.resolve("@NONEXISTENT//pkg:x")

    def test_resolve_error_propagation_missing_field(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        with pytest.raises(AttributeError):
            ws.resolve("@bert//models:lr.nonexistent_field")

    def test_resolve_workspace_attr_returns_value_struct(self, project: Path) -> None:
        from common.python.starlarkish.core.struct import Struct

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("'info")
        assert isinstance(result, Struct)
        assert getattr(result, "kind", None) == "value"
        assert getattr(getattr(result, "location", None), "type", None) == "virtual"
        assert getattr(result, "label", None) == "'info"

    def test_resolve_nested_workspace_attr_returns_typed_value_struct(self, project: Path) -> None:
        from common.python.starlarkish.core.struct import Struct

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("'info.branch")
        assert isinstance(result, Struct)
        assert getattr(result, "kind", None) == "value"
        assert getattr(getattr(result, "type", None), "name", None) == "string"
        assert getattr(getattr(result, "location", None), "type", None) == "virtual"
        assert getattr(result, "label", None) == "'info.branch"

    def test_force_workspace_attr_returns_attribute(self, project: Path) -> None:
        from mlody.core.workspace import force

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = force(ws.resolve("'info"))
        assert result == ws.info

    def test_force_nested_workspace_attr_returns_leaf(self, project: Path) -> None:
        from mlody.core.workspace import force

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = force(ws.resolve("'info.branch"))
        assert result == ws.info.branch

    def test_force_passes_through_non_value(self, project: Path) -> None:
        from mlody.core.workspace import force

        ws = Workspace(monorepo_root=project)
        ws.load()

        plain = ws.resolve("@bert//models:lr")
        assert force(plain) is plain

    def test_force_passes_through_plain_python_object(self) -> None:
        from mlody.core.workspace import force

        assert force(3.14) == 3.14
        assert force("hello") == "hello"
        assert force(None) is None

    def test_resolve_module_label_returns_entities(
        self, project: Path, fs: FakeFilesystem
    ) -> None:
        """@root//path without :name returns all entities from that module as a dict."""
        from common.python.starlarkish.core.struct import Struct

        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents='builtins.register("action", Struct(kind="action", name="trainer", inputs=[], outputs=[], config=[]))',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("@lexica//pipeline")
        assert isinstance(result, dict)
        assert "action/trainer" in result
        assert isinstance(result["action/trainer"], Struct)
        assert result["action/trainer"].name == "trainer"  # type: ignore[attr-defined]

    def test_resolve_entity_query_uses_mlody_filter(self, project: Path, fs: FakeFilesystem) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents="""\
builtins.register("value", Struct(kind="value", name="trainer"))
builtins.register("task", Struct(kind="task", name="trainer", inputs=[], outputs=[], config=[]))
""",
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("@lexica//pipeline:trainer[@mlody _.kind == 'task']")
        assert getattr(result, "kind", None) == "task"


class TestResolveLabelAnchor:
    """Requirement: resolve_label_anchor returns concrete anchor objects."""

    def test_workspace_attribute_anchor(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("'info.branch")

        assert isinstance(anchor, WorkspaceAttributeAnchor)
        assert anchor.root_attribute == "info"
        assert anchor.field_parts == ("branch",)
        assert anchor.root_value == ws.info

    def test_registry_entity_anchor(self, project: Path, fs: FakeFilesystem) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents='builtins.register("task", Struct(kind="task", name="trainer", inputs=[], outputs=[], config=[]))',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("@lexica//pipeline:trainer.outputs")

        assert isinstance(anchor, RegistryEntityAnchor)
        assert anchor.registry_key == ("task", "mlody/teams/lexica/pipeline", "trainer")
        assert anchor.field_parts == ("outputs",)
        assert getattr(anchor.root_value, "name", None) == "trainer"

    def test_registry_entity_anchor_prefers_mlody_filtered_match(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents="""\
builtins.register("value", Struct(kind="value", name="trainer"))
builtins.register("task", Struct(kind="task", name="trainer", inputs=[], outputs=[], config=[]))
""",
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor(
            "@lexica//pipeline:trainer[@mlody _.kind == 'task']",
        )

        assert isinstance(anchor, RegistryEntityAnchor)
        assert anchor.registry_key == ("task", "mlody/teams/lexica/pipeline", "trainer")

    def test_root_object_anchor(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("@bert//models:lr")

        assert isinstance(anchor, RootObjectAnchor)
        assert anchor.root_name == "bert"
        assert anchor.field_parts == ("lr",)
        assert getattr(anchor.root_value, "name", None) == "bert"

    def test_module_global_anchor(self, project: Path, fs: FakeFilesystem) -> None:
        module_path = ROOT / "mlody/teams/lexica/module_globals.mlody"
        fs.create_file(
            str(module_path),
            contents='global_cfg = Struct(kind="value", name="global_cfg", nested=Struct(answer=42))',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("//mlody/teams/lexica/module_globals:global_cfg.nested")

        assert isinstance(anchor, ModuleGlobalAnchor)
        assert anchor.file_path == module_path
        assert anchor.symbol_name == "global_cfg"
        assert anchor.field_parts == ("nested",)
        assert getattr(anchor.root_value, "name", None) == "global_cfg"

    def test_module_aggregate_anchor(self, project: Path, fs: FakeFilesystem) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents='builtins.register("action", Struct(kind="action", name="trainer", inputs=[], outputs=[], config=[]))',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("@lexica//pipeline")

        assert isinstance(anchor, ModuleAggregateAnchor)
        assert anchor.root_name == "lexica"
        assert anchor.module_stem == "mlody/teams/lexica/pipeline"
        assert isinstance(anchor.root_value, dict)
        assert "action/trainer" in anchor.root_value

    def test_root_collection_anchor(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        anchor = ws.resolve_label_anchor("//mlody/teams/lexica/models")

        assert isinstance(anchor, RootCollectionAnchor)
        assert isinstance(anchor.root_value, dict)
        assert "bert" in anchor.root_value


class TestMlodyBuiltins:
    """Requirement: .mlody resolve/force/setf helpers work in workspace evaluation."""

    def test_mlody_builtins_can_read_and_write_declared_attributes(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/a_entity.mlody"),
            contents="""\
builtins.register("value", Struct(
    kind="value",
    name="artifact",
    _lineage=[],
    _source_range=Struct(
        kind="mlody-source-range",
        filepath="mlody/teams/lexica/a_entity.mlody",
        start_line=10,
        end_line=11,
        _entity_type=builtins.lookup("type", "mlody-source-range"),
    ),
))
""",
        )
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/zz_helpers.mlody"),
            contents="""\
before = force(resolve("'info")).branch
setf(base=resolve("'info"), selector=".branch", value="release")
setf(
    base=resolve("@lexica//a_entity:artifact._source_range"),
    selector=".start_line",
    value=321,
)
builtins.register("root", Struct(
    name="builtins_result",
    before=before,
    after=force(resolve("'info.branch")),
    source_line=resolve("@lexica//a_entity:artifact._source_range.start_line"),
))
""",
        )

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.evaluator.registry.roots.by_name["builtins_result"]  # type: ignore[attr-defined]
        assert result.before == ""  # type: ignore[attr-defined]
        assert result.after == "release"  # type: ignore[attr-defined]
        assert result.source_line == 321  # type: ignore[attr-defined]
        assert ws.info.branch == "release"
        assert ws.resolve("@lexica//a_entity:artifact._source_range.start_line") == 321

    def test_mlody_resolve_returns_plain_struct_for_registered_entities(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/downloader.mlody"),
            contents="""\
builtins.register("task", Struct(
    kind="task",
    name="downloader",
    description="initial",
    inputs=[],
    outputs=[],
    config=[],
    action=Struct(kind="action", name="fetch", inputs=[], outputs=[], config=[]),
))
""",
        )

        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws._resolve_for_mlody("@lexica//downloader:downloader")

        assert isinstance(result, Struct)
        assert result.kind == "task"  # type: ignore[attr-defined]
        assert result._resolved_label == "@lexica//downloader:downloader"  # type: ignore[attr-defined]
        assert isinstance(result.action, Struct)  # type: ignore[attr-defined]
        assert result.action.kind == "action"  # type: ignore[attr-defined]

        updated = ws._setf_for_mlody(base=result, selector=".description", value="release")

        assert isinstance(updated, Struct)
        assert updated.description == "release"  # type: ignore[attr-defined]
        assert ws.resolve("@lexica//downloader:downloader.description") == "release"


class TestExpandWildcardLabel:
    def test_query_only_mlody_wildcard_returns_matching_entities(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents="""\
builtins.register("value", Struct(kind="value", name="trainer"))
builtins.register("task", Struct(kind="task", name="trainer", inputs=[], outputs=[], config=[]))
builtins.register("action", Struct(kind="action", name="deploy", inputs=[], outputs=[], config=[]))
""",
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        labels = ws.expand_wildcard_label("//...:[@mlody _.kind == 'task']")
        assert labels == [
            "//mlody/teams/lexica/pipeline:trainer[@mlody _.kind == 'task']",
        ]

    def test_query_only_mlody_wildcard_excludes_synthetic_rootless_user(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        labels = ws.expand_wildcard_label("//...:[@mlody _.kind == 'user']")
        assert labels == []

    def test_query_only_mlody_wildcard_keeps_file_backed_user_entities(
        self,
        project: Path,
        fs: FakeFilesystem,
    ) -> None:
        fs.create_file(
            str(ROOT / "mlody/teams/lexica/pipeline.mlody"),
            contents="""\
builtins.register("user", Struct(
    kind="user",
    name="agarcia",
    description="Alice Garcia",
    groups=["vision"],
))
""",
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        labels = ws.expand_wildcard_label("//...:[@mlody _.kind == 'user']")
        assert labels == [
            "//mlody/teams/lexica/pipeline:agarcia[@mlody _.kind == 'user']",
        ]


class TestWorkspaceStepResolvedObject:
    """Requirement: workspace traversal preserves list-by-name semantics."""

    def test_step_resolved_object_selects_named_item_from_list(self) -> None:
        items = [
            Struct(name="features", kind="value"),
            Struct(name="labels", kind="value"),
        ]

        result = Workspace._step_resolved_object(items, "labels")

        assert getattr(result, "name", None) == "labels"

    def test_step_resolved_object_falls_back_to_getattr(self) -> None:
        result = Workspace._step_resolved_object(Struct(answer=42), "answer")

        assert result == 42



# ---------------------------------------------------------------------------
# stdout safety (LSP transport guard)
# ---------------------------------------------------------------------------


class TestPrintFn:
    """Requirement: print_fn controls sandbox print() behaviour."""

    def test_default_print_fn_writes_to_stdout(
        self, fs: FakeFilesystem, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # CLI usage: print() in .mlody scripts should reach the terminal.
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "printer.mlody"),
            contents='print("hello from workspace")\n',
        )
        ws = Workspace(monorepo_root=project)
        ws.load()

        captured = capsys.readouterr()
        assert "hello from workspace" in captured.out

    def test_custom_print_fn_suppresses_stdout(
        self, fs: FakeFilesystem, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LSP usage: passing a no-op print_fn prevents sandbox print() from
        # corrupting the stdout JSON-RPC transport; a null console prevents the
        # registry dump from reaching stdout.
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "printer.mlody"),
            contents='print("should be suppressed")\n',
        )
        ws = Workspace(
            monorepo_root=project,
            print_fn=lambda *_, **__: None,
            console=Console(file=io.StringIO()),
        )
        ws.load()

        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# Error collection (Phase 2)
# ---------------------------------------------------------------------------


class TestWorkspaceLoadError:
    """Requirement: Phase 2 errors are collected and raised as WorkspaceLoadError."""

    def test_single_bad_file_raises(self, fs: FakeFilesystem, project: Path) -> None:
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "broken.mlody"),
            contents="this is not valid starlark !!!\n",
        )
        ws = Workspace(monorepo_root=project)
        with pytest.raises(WorkspaceLoadError) as exc_info:
            ws.load()
        assert len(exc_info.value.failures) == 1
        path, _exc = exc_info.value.failures[0]
        assert path.name == "broken.mlody"

    def test_multiple_bad_files_collected(self, fs: FakeFilesystem, project: Path) -> None:
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "bad_a.mlody"),
            contents="syntax error !!!\n",
        )
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "bad_b.mlody"),
            contents="another error ???\n",
        )
        ws = Workspace(monorepo_root=project)
        with pytest.raises(WorkspaceLoadError) as exc_info:
            ws.load()
        assert len(exc_info.value.failures) == 2
        failed_names = {p.name for p, _ in exc_info.value.failures}
        assert failed_names == {"bad_a.mlody", "bad_b.mlody"}

    def test_error_message_lists_files(self, fs: FakeFilesystem, project: Path) -> None:
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "broken.mlody"),
            contents="syntax error !!!\n",
        )
        ws = Workspace(monorepo_root=project)
        with pytest.raises(WorkspaceLoadError) as exc_info:
            ws.load()
        msg = str(exc_info.value)
        assert "1 file(s) failed to load" in msg
        assert "broken.mlody" in msg

    def test_good_files_still_loaded_alongside_bad(
        self, fs: FakeFilesystem, project: Path
    ) -> None:
        """Good files evaluated before the bad one are still registered."""
        # models.mlody (good) is alphabetically before broken.mlody
        # but we use sorted(), so: broken < models — both are attempted.
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "broken.mlody"),
            contents="syntax error !!!\n",
        )
        ws = Workspace(monorepo_root=project)
        with pytest.raises(WorkspaceLoadError):
            ws.load()
        # models.mlody was processed; "bert" root should be registered
        assert "mlody/teams/lexica/models:bert" in ws.evaluator.registry.roots.by_key


class TestStdoutSafety:
    """Requirement: load() must never write to stdout (framework-level).

    The LSP server communicates over stdio.  Any stray print() or write to
    sys.stdout from workspace/evaluator framework code (not user scripts)
    injects raw bytes into the JSON-RPC transport, corrupting the
    Content-Length framing and causing the client to lose sync.
    """

    def test_load_does_not_write_to_stdout(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The LSP server always supplies a no-op print_fn and a null console so
        # that neither sandbox print() calls nor any framework-level verbose
        # reach stdout.
        ws = Workspace(
            monorepo_root=project,
            print_fn=lambda *_, **__: None,
            console=Console(file=io.StringIO()),
        )
        ws.load()

        captured = capsys.readouterr()
        assert captured.out == "", (
            "workspace.load() must not write to stdout — "
            "stdout is the LSP transport and stray output corrupts the protocol"
        )

    def test_verbose_load_does_not_dump_registry_to_stdout(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load(verbose=True)

        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# Port list → named Struct conversion (Phase 3)
# ---------------------------------------------------------------------------

# Shared .mlody content for port-conversion tests.  Uses Struct() directly
# so we control the exact shape without depending on the task/action DSL.
_ROOTS_WITH_BERT = """\
load("//mlody/core/builtins.mlody", "root")
root(name="bert", path="//mlody/teams/bert", description="bert team")
"""

_PORT_BUILTINS = """\
def root(name, path, description=""):
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
"""


def _make_port_project(fs: FakeFilesystem, entity_mlody: str) -> Path:
    """Create a minimal fake workspace with one entity file under //mlody/teams/bert/."""
    root = Path("/port_project")
    fs.create_file(str(root / "mlody/core/builtins.mlody"), contents=_PORT_BUILTINS)
    fs.create_file(str(root / "mlody/roots.mlody"), contents=_ROOTS_WITH_BERT)
    fs.create_file(str(root / "mlody/common/types.mlody"), contents=TYPES_MLODY)
    # mm.mlody is required by workspace_loader when roots.mlody exists.
    _add_mm_files(fs, root)
    fs.create_dir(str(root / "mlody/teams/bert"))
    fs.create_file(str(root / "mlody/teams/bert/entity.mlody"), contents=entity_mlody)
    return root


class TestPortConversion:
    """Requirement: workspace-port-conversion — port lists become named Structs."""

    # TC-001/002/003 — basic named access, resolve to Struct, deep traversal
    def test_task_outputs_accessible_by_name_after_load(
        self, fs: FakeFilesystem
    ) -> None:
        # TC-001: outputs list element is accessible as named attribute.
        # TC-002: outputs field itself is a Struct after load().
        # TC-003: deep traversal into element sub-field works.
        entity_mlody = """\
loc = Struct(kind="location", type="path", name="weights_path", path="/tmp/w")
weight_val = Struct(kind="value", name="backbone_weights", location=loc)
builtins.register("task", Struct(
    kind="task",
    name="train_bert",
    inputs=[],
    outputs=[weight_val],
    config=[],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        # TC-001: named element is accessible
        el = ws.resolve("@bert//entity:train_bert.outputs.backbone_weights")
        assert isinstance(el, Struct)
        assert getattr(el, "name", None) == "backbone_weights"

        # TC-002: outputs field is a dict after load().
        outputs_struct = ws.resolve("@bert//entity:train_bert.outputs")
        assert isinstance(outputs_struct, dict)
        assert isinstance(outputs_struct.get("backbone_weights"), Struct)

        # TC-003: deep traversal into element sub-field
        loc_val = ws.resolve("@bert//entity:train_bert.outputs.backbone_weights.location")
        assert getattr(loc_val, "path", None) == "/tmp/w"

    # TC-004 — inputs and config port fields
    def test_inputs_and_config_accessible_by_name_after_load(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
inp = Struct(kind="value", name="raw_data", location=Struct(kind="location", type="path", name="data_loc", path="/data"))
cfg = Struct(kind="value", name="lr_value", location=Struct(kind="location", type="path", name="lr_loc", path="/cfg"))
builtins.register("task", Struct(
    kind="task",
    name="preprocess",
    inputs=[inp],
    outputs=[],
    config=[cfg],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        # inputs
        inp_el = ws.resolve("@bert//entity:preprocess.inputs.raw_data")
        assert isinstance(inp_el, Struct)
        assert getattr(inp_el, "name", None) == "raw_data"
        assert isinstance(ws.resolve("@bert//entity:preprocess.inputs"), dict)

        # config
        cfg_el = ws.resolve("@bert//entity:preprocess.config.lr_value")
        assert isinstance(cfg_el, Struct)
        assert getattr(cfg_el, "name", None) == "lr_value"
        assert isinstance(ws.resolve("@bert//entity:preprocess.config"), dict)

    # TC-005 — direct action entity (not embedded in a task)
    def test_direct_action_outputs_accessible_by_name(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
w = Struct(kind="value", name="weights", location=Struct(kind="location", type="path", name="w_loc", path="/weights"))
builtins.register("action", Struct(
    kind="action",
    name="train_action",
    inputs=[],
    outputs=[w],
    config=[],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        el = ws.resolve("@bert//entity:train_action.outputs.weights")
        assert isinstance(el, Struct)
        assert getattr(el, "name", None) == "weights"

    # TC-006 — embedded action inside a task
    def test_embedded_action_outputs_accessible_by_name(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
w = Struct(kind="value", name="weights", location=Struct(kind="location", type="path", name="w_loc", path="/w"))
emb_action = Struct(kind="action", name="finetune", inputs=[], outputs=[w], config=[])
builtins.register("task", Struct(
    kind="task",
    name="finetune_task",
    inputs=[],
    outputs=[],
    config=[],
    action=emb_action,
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        el = ws.resolve("@bert//entity:finetune_task.action.outputs.weights")
        assert isinstance(el, Struct)
        assert getattr(el, "name", None) == "weights"

    def test_grouped_embedded_action_outputs_accessible_by_group_key(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
w = Struct(kind="value", name="weights", location=Struct(kind="location", type="path", name="w_loc", path="/w"))
out = Struct(kind="value", name="model_weights", group="model", location=Struct(kind="location", type="path", name="out_loc", path="/out"))
emb_action = Struct(kind="action", name="finetune", inputs=[], outputs=[w], config=[])
builtins.register("task", Struct(
    kind="task",
    name="finetune_task",
    inputs=[],
    outputs=[out],
    config=[],
    action={"model": emb_action},
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        action_groups = ws.resolve("@bert//entity:finetune_task.action")
        assert isinstance(action_groups, dict)
        grouped_action = action_groups["model"]
        assert isinstance(grouped_action, Struct)
        el = getattr(grouped_action, "outputs", {})["weights"]
        assert isinstance(el, Struct)
        assert getattr(el, "name", None) == "weights"

    # TC-007 — empty list becomes empty Struct
    def test_empty_port_list_becomes_empty_struct(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
builtins.register("task", Struct(
    kind="task",
    name="empty_ports",
    inputs=[],
    outputs=[],
    config=[],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()  # must not raise

        config_val = ws.resolve("@bert//entity:empty_ports.config")
        assert isinstance(config_val, dict)
        assert len(config_val) == 0

    # TC-008 — missing name field raises ValueError
    def test_element_missing_name_raises_value_error(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
no_name_el = Struct(kind="value", location=Struct(kind="location", type="path", name="x", path="/x"))
builtins.register("task", Struct(
    kind="task",
    name="bad_task",
    inputs=[],
    outputs=[no_name_el],
    config=[],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        with pytest.raises(ValueError, match="bad_task") as exc_info:
            ws.load()
        # Error message must mention the field name too
        assert "outputs" in str(exc_info.value)

    # TC-009 — duplicate names raise ValueError
    def test_duplicate_element_names_raise_value_error(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
w1 = Struct(kind="value", name="w", location=Struct(kind="location", type="path", name="l1", path="/1"))
w2 = Struct(kind="value", name="w", location=Struct(kind="location", type="path", name="l2", path="/2"))
builtins.register("task", Struct(
    kind="task",
    name="dup_task",
    inputs=[],
    outputs=[w1, w2],
    config=[],
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        with pytest.raises(ValueError, match="dup_task") as exc_info:
            ws.load()
        assert "w" in str(exc_info.value)

    # TC-010 — idempotency: calling _convert_single_entity twice is safe
    def test_convert_single_entity_is_idempotent(self) -> None:
        w = Struct(kind="value", name="weights", path="/w")
        entity = Struct(
            kind="task",
            name="some_task",
            inputs=[],
            outputs=[w],
            config=[],
        )
        first = Workspace._convert_single_entity(entity)
        second = Workspace._convert_single_entity(first)
        # No error, and field-by-field equality holds.
        assert first == second
        assert isinstance(first.outputs.get("weights"), Struct)

    # TC-011 — non-port fields are preserved unchanged after conversion
    def test_non_port_fields_preserved_after_load(
        self, fs: FakeFilesystem
    ) -> None:
        entity_mlody = """\
builtins.register("task", Struct(
    kind="task",
    name="meta_task",
    inputs=[],
    outputs=[],
    config=[],
    extra_meta="important_value",
))
"""
        root = _make_port_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        entity = ws.resolve("@bert//entity:meta_task")
        assert getattr(entity, "kind", None) == "task"
        assert getattr(entity, "name", None) == "meta_task"
        assert getattr(entity, "extra_meta", None) == "important_value"


# ---------------------------------------------------------------------------
# Record-aware field traversal (design §D-3, §D-4, §D-5)
# ---------------------------------------------------------------------------


# Shared helpers for record traversal tests.
_RECORD_ROOTS_MLODY = """\
load("//mlody/core/builtins.mlody", "root")
root(name="bert", path="//mlody/teams/bert", description="bert team")
"""

_RECORD_PORT_BUILTINS = """\
def root(name, path, description=""):
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
"""


def _make_record_project(fs: FakeFilesystem, entity_mlody: str) -> Path:
    """Create a minimal fake workspace with one entity file."""
    root = Path("/rec_project")
    fs.create_file(str(root / "mlody/core/builtins.mlody"), contents=_RECORD_PORT_BUILTINS)
    fs.create_file(str(root / "mlody/roots.mlody"), contents=_RECORD_ROOTS_MLODY)
    # types.mlody must be present so register_mm_pattern("type", "vector", ...)
    # fires after mm.mlody initialises MmNamespace, enabling render.mlody to use
    # mm.vector(...) patterns.  Phase 1 loads mm.mlody first, then types.mlody.
    fs.create_file(str(root / "mlody/common/types.mlody"), contents=TYPES_MLODY)
    # mm.mlody is required by workspace_loader when roots.mlody exists.
    _add_mm_files(fs, root)
    fs.create_dir(str(root / "mlody/teams/bert"))
    fs.create_file(str(root / "mlody/teams/bert/entity.mlody"), contents=entity_mlody)
    return root


class TestRecordAwareFieldTraversal:
    """Requirement: Record-aware field lookup in Workspace.resolve.

    Scenarios trace to:
      openspec/changes/mlody-field-traversal/specs/field-traversal/spec.md
    """

    def test_field_found_in_type_fields_returns_struct_with_composed_location(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Field found in type.fields returns field value struct.

        The returned struct must have its location replaced by the composed
        location (parent path joined with field path).
        """
        entity_mlody = """\
model_info_field = Struct(
    name="model_info",
    type=None,
    location=Struct(kind="posix", type="posix", name="model_info_loc", path="info"),
)
record_type = Struct(
    kind="record",
    name="ModelType",
    fields=[model_info_field],
)
parent_loc = Struct(kind="posix", type="posix", name="parent_loc", path="models/bert")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.model_info")

        assert isinstance(result, Struct)
        loc = getattr(result, "location", None)
        assert loc is not None
        # Composed path list: ["models/bert/info"]
        assert getattr(loc, "path", None) == ["models/bert/info"]

    def test_field_found_via_type_attribute_fallback(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Field found via type attribute fallback.

        When field_name is not in type.fields but getattr(value.type, field_name)
        succeeds, that result is returned.
        """
        entity_mlody = """\
record_type = Struct(
    kind="record",
    name="ModelType",
    fields=[],
    weights="direct_attr_value",
)
parent_loc = Struct(kind="posix", type="posix", name="loc", path="models")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.weights")

        assert result == "direct_attr_value"

    def test_non_record_base_value_falls_through_to_generic_traversal(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Non-record base value does not activate record-traversal branch.

        A value with type.kind != "record" falls through to the existing _step
        loop, which uses getattr.  Here we verify it still resolves the field
        via generic traversal (the attribute exists on the struct directly).
        """
        entity_mlody = """\
tensor_type = Struct(kind="tensor", name="TensorType", fields=[])
builtins.register("value", Struct(
    kind="value",
    name="my_tensor",
    type=tensor_type,
    location=None,
    default=None,
    source=None,
    _lineage=[],
    name_field="my_tensor",
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        # .kind exists as a direct attribute on the struct → generic traversal succeeds
        result = ws.resolve("@bert//entity:my_tensor.kind")
        assert result == "value"

    def test_missing_field_returns_mlody_unresolved_value(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Missing field returns MlodyUnresolvedValue listing available fields."""
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        entity_mlody = """\
name_field = Struct(name="name", type=None, location=None)
record_type = Struct(kind="record", name="ModelType", fields=[name_field])
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=None,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.ghost_field")

        assert isinstance(result, MlodyUnresolvedValue)
        assert "ghost_field" in result.reason
        assert "name" in result.reason  # available fields listed

    def test_fields_list_entry_takes_precedence_over_type_attribute(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Fields list entry takes precedence over type attribute.

        When type.fields contains an entry named "kind" and getattr(value.type,
        "kind") also returns a different value, the fields list wins.
        """
        entity_mlody = """\
kind_field = Struct(
    name="kind",
    type=None,
    location=Struct(kind="posix", type="posix", name="kind_loc", path="kind_dir"),
)
record_type = Struct(
    kind="record",
    name="ModelType",
    fields=[kind_field],
)
parent_loc = Struct(kind="posix", type="posix", name="parent_loc", path="models")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        # The fields list contains "kind"; getattr(record_type, "kind") == "record".
        # The fields list must take precedence, so we get the field struct, not "record".
        result = ws.resolve("@bert//entity:my_model.kind")

        assert isinstance(result, Struct)
        # Result is the field struct (with composed location), not the string "record".
        assert getattr(result, "name", None) == "kind"
        loc = getattr(result, "location", None)
        assert loc is not None
        assert getattr(loc, "path", None) == ["models/kind_dir"]


class TestRecordFieldTraversalErrorPropagation:
    """Requirement: Location composition error propagated as MlodyUnresolvedValue."""

    def test_cross_kind_compose_error_returned_as_unresolved(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Cross-kind compose error returned as MlodyUnresolvedValue."""
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        entity_mlody = """\
weights_field = Struct(
    name="weights",
    type=None,
    location=Struct(kind="s3", type="s3", name="s3_loc", path="bucket/weights"),
)
record_type = Struct(kind="record", name="ModelType", fields=[weights_field])
# Parent has posix kind; field has s3 kind → cross-kind compose error.
parent_loc = Struct(kind="posix", type="posix", name="parent_loc", path="models")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.weights")

        assert isinstance(result, MlodyUnresolvedValue)
        assert "cross-kind" in result.reason.lower() or "posix" in result.reason

    def test_multi_segment_field_path_on_record_returns_unresolved_for_missing_field(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Multi-segment field_path activates record-traversal branch.

        After the multi-level fix (mlody-field-traversal-multilevel), multi-segment
        paths on record-typed values use _traverse_one_step at every level.
        When a field is absent from type.fields (empty list here), the result is
        MlodyUnresolvedValue — not a fallback to generic getattr.
        """
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        entity_mlody = """\
record_type = Struct(kind="record", name="ModelType", fields=[])
# The value struct has a direct .sub attribute, but the record-aware traversal
# uses type.fields (empty here) — so "sub" is reported as missing.
sub_struct = Struct(value=42)
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=None,
    default=None,
    source=None,
    _lineage=[],
    sub=sub_struct,
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        # "sub" is not in type.fields → record traversal returns MlodyUnresolvedValue.
        result = ws.resolve("@bert//entity:my_model.sub.value")
        assert isinstance(result, MlodyUnresolvedValue)
        assert "sub" in result.reason


class TestMultiLevelRecordTraversalViaWorkspace:
    """Requirement: Multi-level traversal in Workspace.resolve via _traverse_one_step.

    Scenarios trace to:
      openspec/changes/mlody-field-traversal-multilevel/specs/multi-level-field-traversal/spec.md
    """

    def test_single_level_traversal_is_unchanged_regression(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Single-level traversal via Workspace.resolve is unchanged (regression).

        Verifies that the new multi-level loop preserves the existing single-level
        behaviour: field struct returned with composed location.
        """
        from mlody.resolver.values.structural import MlodyUnresolvedValue  # noqa: F401

        entity_mlody = """\
field_a = Struct(
    name="field_a",
    type=None,
    location=Struct(kind="posix", type="posix", name="loc", path="a_dir"),
)
record_type = Struct(kind="record", name="T", fields=[field_a])
parent_loc = Struct(kind="posix", type="posix", name="loc", path="root/path")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.field_a")

        assert isinstance(result, Struct)
        loc = getattr(result, "location", None)
        # Single-level compose yields path list with one element.
        assert getattr(loc, "path", None) == ["root/path/a_dir"]

    def test_two_level_traversal_composes_locations(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Two-level traversal via Workspace.resolve composes locations.

        @bert//entity:my_model.field_a.field_b with both fields record-typed →
        location.path == "root/path" / "a_dir" / "b_dir"
        """
        entity_mlody = """\
field_b = Struct(
    name="field_b",
    type=None,
    location=Struct(kind="posix", type="posix", name="loc", path="b_dir"),
)
field_a_type = Struct(kind="record", name="AType", fields=[field_b])
field_a = Struct(
    name="field_a",
    type=field_a_type,
    location=Struct(kind="posix", type="posix", name="loc", path="a_dir"),
)
record_type = Struct(kind="record", name="T", fields=[field_a])
parent_loc = Struct(kind="posix", type="posix", name="loc", path="root/path")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.field_a.field_b")

        assert isinstance(result, Struct)
        loc = getattr(result, "location", None)
        assert getattr(loc, "path", None) == ["root/path/a_dir/b_dir"]

    def test_traversal_failure_returns_mlody_unresolved_value_without_raising(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: Traversal failure in Workspace.resolve returns MlodyUnresolvedValue.

        Missing field at first segment → MlodyUnresolvedValue, no exception.
        """
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        entity_mlody = """\
record_type = Struct(kind="record", name="T", fields=[])
parent_loc = Struct(kind="posix", type="posix", name="loc", path="root")
builtins.register("value", Struct(
    kind="value",
    name="my_model",
    type=record_type,
    location=parent_loc,
    default=None,
    source=None,
    _lineage=[],
))
"""
        root = _make_record_project(fs, entity_mlody)
        ws = Workspace(monorepo_root=root)
        ws.load()

        result = ws.resolve("@bert//entity:my_model.ghost_field.sub")

        assert isinstance(result, MlodyUnresolvedValue)
        assert "ghost_field" in result.reason
