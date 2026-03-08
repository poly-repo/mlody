"""Tests for mlody.cli.show — show subcommand and show_fn."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from common.python.starlarkish.core.struct import struct

from mlody.cli.main import cli
from mlody.cli.show import show_fn
from mlody.resolver.errors import UnknownRefError, WorkspaceResolutionError


# ---------------------------------------------------------------------------
# show_fn — functional form
# ---------------------------------------------------------------------------


class TestShowFn:
    """Requirement: show_fn accepts a label and resolves via resolve_workspace."""

    def test_single_cwd_label_resolves_value(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = 0.001

        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, None)
            result = show_fn("@bert//:lr", monorepo_root=tmp_path)

        assert result == 0.001

    def test_resolve_workspace_called_with_label_and_root(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = 42

        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, None)
            show_fn("@bert//:lr", monorepo_root=tmp_path)

        mock_rw.assert_called_once_with(
            "@bert//:lr",
            monorepo_root=tmp_path,
            roots_file=None,
            print_fn=print,
        )

    def test_workspace_resolve_called_with_inner_label(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = 99

        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, None)
            show_fn("@bert//:lr", monorepo_root=tmp_path)

        mock_ws.resolve.assert_called_once_with("@bert//:lr")

    def test_committoid_label_uses_inner_label_for_resolve(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "value"

        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, "a" * 40)
            show_fn("main|@bert//:lr", monorepo_root=tmp_path)

        # resolve() must be called with the inner label, not the full label
        mock_ws.resolve.assert_called_once_with("@bert//:lr")


# ---------------------------------------------------------------------------
# CLI show command — cwd target
# ---------------------------------------------------------------------------


class TestShowCommandCwdTarget:
    """Requirement: cwd target resolves against cwd workspace."""

    def test_cwd_target_resolves_and_prints(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = 0.001
        mock_ws.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, None)
            result = runner.invoke(
                cli,
                ["show", "@bert//:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "0.001" in result.output

    def test_cwd_target_with_legacy_workspace_injection(self) -> None:
        # Existing tests inject workspace — this legacy path must still work
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output


# ---------------------------------------------------------------------------
# CLI show command — committoid target
# ---------------------------------------------------------------------------


class TestShowCommandCommittoidTarget:
    """Requirement: committoid-qualified target resolves against cached workspace."""

    def test_committoid_target_calls_resolve_workspace_with_full_label(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "result"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, "a" * 40)
            result = runner.invoke(
                cli,
                ["show", "main|@bert//:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        mock_rw.assert_called_once_with(
            "main|@bert//:lr",
            monorepo_root=tmp_path,
            roots_file=None,
        )

    def test_committoid_target_calls_workspace_resolve_with_inner_label(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "value"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, "a" * 40)
            runner.invoke(
                cli,
                ["show", "main|@bert//:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        # workspace.resolve must be called with the inner label only
        mock_ws.resolve.assert_called_once_with("@bert//:lr")


# ---------------------------------------------------------------------------
# CLI show command — mixed targets
# ---------------------------------------------------------------------------


class TestShowCommandMixedTargets:
    """Requirement: mixed cwd and committoid targets coexist."""

    def test_mixed_targets_printed_in_order(self, tmp_path: Path) -> None:
        mock_ws_cwd = MagicMock()
        mock_ws_cwd.resolve.return_value = "from-cwd"
        mock_ws_cwd.root_infos = {}

        mock_ws_commit = MagicMock()
        mock_ws_commit.resolve.return_value = "from-main"
        mock_ws_commit.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.side_effect = [(mock_ws_cwd, None), (mock_ws_commit, "a" * 40)]
            result = runner.invoke(
                cli,
                ["show", "@bert//:lr", "main|@bert//:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        cwd_pos = result.output.index("from-cwd")
        commit_pos = result.output.index("from-main")
        assert cwd_pos < commit_pos


# ---------------------------------------------------------------------------
# CLI show command — verbose mode
# ---------------------------------------------------------------------------


class TestShowCommandVerbose:
    """Requirement: verbose mode emits resolved SHA at DEBUG level."""

    def test_verbose_logs_resolved_sha(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        full_sha = "a" * 40
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "val"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with caplog.at_level(logging.DEBUG, logger="mlody.cli.show"):
            with patch("mlody.cli.show.resolve_workspace") as mock_rw:
                mock_rw.return_value = (mock_ws, full_sha)
                runner.invoke(
                    cli,
                    ["--verbose", "show", "main|@bert//:lr"],
                    obj={"monorepo_root": tmp_path, "roots": None, "verbose": True},
                )

        assert any(full_sha in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI show command — output rendering
# ---------------------------------------------------------------------------


class TestShowCommandOutput:
    """Requirement: Resolve and display target values."""

    def test_primitive_value_displayed_as_plain_string(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output

    def test_struct_value_displayed_via_pretty_repr(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = struct(name="bert", lr=0.001)
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//:config"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "bert" in result.output
        assert "0.001" in result.output

    def test_multiple_targets_displayed_in_order(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, "adam"]
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@bert//:lr", "@bert//:opt"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 0
        lr_pos = result.output.index("0.001")
        opt_pos = result.output.index("adam")
        assert lr_pos < opt_pos


# ---------------------------------------------------------------------------
# CLI show command — error handling
# ---------------------------------------------------------------------------


class TestShowCommandErrors:
    """Requirement: Clear error messages for resolution failures."""

    def test_missing_root_shows_error_with_available_roots(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = KeyError("NONEXISTENT")
        ws.root_infos = {"lexica": MagicMock(), "common": MagicMock()}

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@NONEXISTENT//:x"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 1
        assert "NONEXISTENT" in result.stderr
        assert "Available roots:" in result.stderr
        assert "lexica" in result.stderr

    def test_missing_field_shows_error(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = AttributeError("'Struct' object has no attribute 'bad_field'")
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//:bad_field"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 1
        assert "bad_field" in result.stderr

    def test_partial_failure_shows_successes_and_errors(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, KeyError("MISSING")]
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@bert//:lr", "@MISSING//:x"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 1
        assert "0.001" in result.output
        assert "MISSING" in result.stderr

    def test_workspace_resolution_error_printed_to_stderr_and_exit_1(
        self, tmp_path: Path
    ) -> None:
        # Scenario: resolver exception causes target to print error and continue
        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.side_effect = UnknownRefError("nosuchref", "origin")
            result = runner.invoke(
                cli,
                ["show", "nosuchref|@bert//:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "nosuchref" in result.stderr or "nosuchref" in result.output

    def test_resolver_exception_continues_to_next_target(self, tmp_path: Path) -> None:
        # Scenario: processing continues for remaining targets after resolver error
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "ok-value"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.side_effect = [
                UnknownRefError("bad", "origin"),
                (mock_ws, None),
            ]
            result = runner.invoke(
                cli,
                ["show", "bad|@bert//:lr", "@bert//:good"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "ok-value" in result.output


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


class TestShowRegistration:
    """Requirement: main() imports show to register subcommand."""

    def test_show_appears_in_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "show" in result.output
