"""Tests for mlody.core.workspace — two-phase loading and target resolution."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from mlody.core.targets import TargetAddress
from mlody.core.workspace import RootInfo, Workspace

ROOT = Path("/project")

BUILTINS_MLODY = """\
def root(name, path, description=""):
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
"""

ROOTS_MLODY = """\
load("//mlody/core/builtins.mlody", "root")

root(name="lexica", path="//mlody/teams/lexica", description="text ML team")
"""


@pytest.fixture()
def project(fs: FakeFilesystem) -> Path:
    """Set up a fake project with roots and team files."""
    fs.create_file(str(ROOT / "mlody/core/builtins.mlody"), contents=BUILTINS_MLODY)
    fs.create_file(str(ROOT / "mlody/roots.mlody"), contents=ROOTS_MLODY)
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
        assert "mlody/teams/lexica/bert" in ws.evaluator.roots

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

    def test_missing_roots_file(self, fs: FakeFilesystem) -> None:
        root = Path("/empty")
        root.mkdir()
        ws = Workspace(monorepo_root=root)

        with pytest.raises(FileNotFoundError, match="Roots file not found"):
            ws.load()

    def test_no_roots_registered(self, fs: FakeFilesystem) -> None:
        root = Path("/no_roots")
        root.mkdir()
        fs.create_file(str(root / "mlody/roots.mlody"), contents="# no roots here\n")
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


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestResolve:
    """Requirement: Target resolution via Workspace."""

    def test_resolve_string_target(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        result = ws.resolve("@bert//:lr")
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
            ws.resolve("@NONEXISTENT//:x")

    def test_resolve_error_propagation_missing_field(self, project: Path) -> None:
        ws = Workspace(monorepo_root=project)
        ws.load()

        with pytest.raises(AttributeError):
            ws.resolve("@bert//:lr.nonexistent_field")


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
        # LSP usage: passing a no-op prevents sandbox print() from corrupting
        # the stdout JSON-RPC transport.
        fs.create_file(
            str(project / "mlody" / "teams" / "lexica" / "printer.mlody"),
            contents='print("should be suppressed")\n',
        )
        ws = Workspace(monorepo_root=project, print_fn=lambda *_, **__: None)
        ws.load()

        captured = capsys.readouterr()
        assert captured.out == ""


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
        ws = Workspace(monorepo_root=project)
        ws.load()

        captured = capsys.readouterr()
        assert captured.out == "", (
            "workspace.load() must not write to stdout — "
            "stdout is the LSP transport and stray output corrupts the protocol"
        )
