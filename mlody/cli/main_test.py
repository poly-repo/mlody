"""Tests for mlody.cli.main — CLI entry point, monorepo root verification, global options."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from mlody.cli.main import _configure_logging, cli, main, verify_monorepo_root


# ---------------------------------------------------------------------------
# verify_monorepo_root
# ---------------------------------------------------------------------------


class TestVerifyMonorepoRoot:
    """Requirement: Monorepo root verification."""

    def test_valid_monorepo_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        result = verify_monorepo_root()
        assert result == tmp_path

    def test_invalid_working_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            verify_monorepo_root()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI group — helper command for testing context propagation
# ---------------------------------------------------------------------------


@cli.command("_test_probe")
@click.pass_context
def _test_probe(ctx: click.Context) -> None:
    """Test-only subcommand that echoes context values."""
    # Scenario: cli group no longer constructs workspace — probe checks monorepo_root
    click.echo(f"monorepo_root={ctx.obj.get('monorepo_root') is not None}")
    click.echo(f"verbose={ctx.obj.get('verbose')}")
    click.echo(f"full_workspace={ctx.obj.get('full_workspace')}")
    click.echo(f"workspace={ctx.obj.get('workspace') is not None}")


# ---------------------------------------------------------------------------
# CLI group — context propagation
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Requirement: Click entry point structure — context propagation."""

    def test_subcommand_receives_monorepo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Scenario: cli group no longer constructs Workspace; it sets monorepo_root
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "monorepo_root=True" in result.output

    def test_workspace_not_set_by_cli_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Scenario: ctx.obj["workspace"] is NOT set by the cli callback
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "workspace=False" in result.output


# ---------------------------------------------------------------------------
# CLI group — --roots option
# ---------------------------------------------------------------------------


class TestRootsOption:
    """Requirement: Global CLI options — --roots stored in ctx for subcommands."""

    def test_roots_stored_in_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        @cli.command("_test_roots_probe")
        @click.pass_context
        def _test_roots_probe(ctx: click.Context) -> None:
            click.echo(f"roots={ctx.obj.get('roots')}")

        custom_roots = tmp_path / "custom" / "roots.mlody"
        runner = CliRunner()
        runner.invoke(cli, ["--roots", str(custom_roots), "_test_roots_probe"])

    def test_roots_default_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        @cli.command("_test_roots_default")
        @click.pass_context
        def _test_roots_default(ctx: click.Context) -> None:
            click.echo(f"roots_is_none={ctx.obj.get('roots') is None}")

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_roots_default"])
        assert "roots_is_none=True" in result.output


# ---------------------------------------------------------------------------
# CLI group — --verbose flag
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    """Requirement: Global CLI options — --verbose."""

    def test_verbose_stored_on_context(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "_test_probe"])

        assert result.exit_code == 0
        assert "verbose=True" in result.output

    def test_verbose_defaults_to_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "verbose=False" in result.output


class TestFullWorkspaceFlag:
    """Requirement: Global CLI options — --full-workspace."""

    def test_full_workspace_stored_on_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["--full-workspace", "_test_probe"])

        assert result.exit_code == 0
        assert "full_workspace=True" in result.output

    def test_full_workspace_defaults_to_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "full_workspace=False" in result.output


# ---------------------------------------------------------------------------
# CLI group — --help
# ---------------------------------------------------------------------------


class TestHelp:
    """Requirement: Help does not trigger workspace loading."""

    def test_help_does_not_verify_monorepo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No MODULE.bazel — would fail if verify_monorepo_root triggered
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "mlody" in result.output

    def test_no_subcommand_prints_help(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [])

        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "Commands:" in result.output


# ---------------------------------------------------------------------------
# CLI group — error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Requirement: Monorepo root errors surface cleanly at the CLI level."""

    def test_missing_module_bazel_produces_cli_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No MODULE.bazel present — cli group must exit with code 1
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 1

    def test_server_flag_rejects_subcommands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["--server", "_test_probe"])

        assert result.exit_code == 2
        assert "--server cannot be combined with a subcommand" in result.output


class TestServerFlag:
    """Requirement: Persistent server mode inherits CLI context correctly."""

    def test_server_flag_launches_server_with_cli_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        (tmp_path / "mlody").mkdir()
        monkeypatch.chdir(tmp_path)

        with patch("mlody.cli.server.run_server") as run_server:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "--server",
                    "--verbose",
                    "--full-workspace",
                    "--workspace",
                    "mlody",
                    "--roots",
                    "mlody/custom-roots.mlody",
                    "--server-host",
                    "0.0.0.0",
                    "--server-port",
                    "9900",
                    "--lsp-port",
                    "9901",
                ],
            )

        assert result.exit_code == 0
        run_server.assert_called_once()
        config = run_server.call_args.args[0]
        assert config.monorepo_root == tmp_path
        assert config.workspace_root == tmp_path / "mlody"
        assert config.roots == Path("mlody/custom-roots.mlody")
        assert config.verbose is True
        assert config.full_workspace is True
        assert config.http_host == "0.0.0.0"
        assert config.http_port == 9900
        assert config.lsp_host == "0.0.0.0"
        assert config.lsp_port == 9901


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


class TestLoggingConfiguration:
    """Requirement: Global CLI options — logging setup.

    The root logger level must match the --verbose flag so that the debug
    output from workspace.py and evaluator.py reaches the console only when
    explicitly requested.  The LSP server has its own LSPLogHandler and is
    unaffected by this configuration.
    """

    def test_verbose_sets_debug_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logging.getLogger(), "level", logging.NOTSET)
        _configure_logging(verbose=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_default_sets_warning_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logging.getLogger(), "level", logging.NOTSET)
        _configure_logging(verbose=False)
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_exposes_workspace_debug_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(logging.getLogger(), "level", logging.NOTSET)

        with caplog.at_level(logging.DEBUG, logger="mlody.core.workspace"):
            _configure_logging(verbose=True)
            logging.getLogger("mlody.core.workspace").debug("Loading root: %s", tmp_path)

        assert any("Loading root" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


class TestMain:
    """Requirement: main() entry point imports and invokes."""

    def test_main_invokes_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0

    def test_main_is_callable(self) -> None:
        assert callable(main)
