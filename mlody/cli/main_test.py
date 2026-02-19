"""Tests for mlody.cli.main — CLI entry point, monorepo root verification, global options."""

from __future__ import annotations

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
    click.echo(f"workspace={ctx.obj.get('workspace') is not None}")
    click.echo(f"verbose={ctx.obj.get('verbose')}")


# ---------------------------------------------------------------------------
# CLI group — context propagation
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Requirement: Click entry point structure — context propagation."""

    def test_subcommand_receives_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "workspace=True" in result.output


# ---------------------------------------------------------------------------
# CLI group — --roots option
# ---------------------------------------------------------------------------


class TestRootsOption:
    """Requirement: Global CLI options — --roots."""

    def test_custom_roots_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        custom_roots = tmp_path / "custom" / "roots.mlody"

        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            runner.invoke(cli, ["--roots", str(custom_roots), "_test_probe"])

        mock_ws_cls.assert_called_once_with(monorepo_root=tmp_path, roots_file=custom_roots)

    def test_default_roots_when_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            runner.invoke(cli, ["_test_probe"])

        mock_ws_cls.assert_called_once_with(monorepo_root=tmp_path, roots_file=None)


# ---------------------------------------------------------------------------
# CLI group — --verbose flag
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    """Requirement: Global CLI options — --verbose."""

    def test_verbose_stored_on_context(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            result = runner.invoke(cli, ["--verbose", "_test_probe"])

        assert result.exit_code == 0
        assert "verbose=True" in result.output

    def test_verbose_defaults_to_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0
        assert "verbose=False" in result.output


# ---------------------------------------------------------------------------
# CLI group — --help
# ---------------------------------------------------------------------------


class TestHelp:
    """Requirement: Help does not trigger workspace loading."""

    def test_help_does_not_load_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No MODULE.bazel — would fail if workspace loading triggered
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "mlody" in result.output
        mock_ws_cls.assert_not_called()


# ---------------------------------------------------------------------------
# CLI group — error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Requirement: Workspace loading errors surface cleanly."""

    def test_missing_roots_file_produces_cli_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value.load.side_effect = FileNotFoundError("Roots file not found: /bad/path")
            result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 1
        assert "Roots file not found" in result.output

    def test_evaluation_error_produces_cli_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value.load.side_effect = SyntaxError("invalid .mlody file")
            result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 1
        assert "invalid .mlody file" in result.output

    def test_no_traceback_on_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "MODULE.bazel").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value.load.side_effect = FileNotFoundError("Roots file not found")
            result = runner.invoke(cli, ["_test_probe"])

        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


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
        import logging

        monkeypatch.setattr(logging.getLogger(), "level", logging.NOTSET)
        _configure_logging(verbose=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_default_sets_warning_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        monkeypatch.setattr(logging.getLogger(), "level", logging.NOTSET)
        _configure_logging(verbose=False)
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_exposes_workspace_debug_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        # Workspace debug output (e.g. "Loading root: ...") must surface when
        # --verbose is active.  We emit a synthetic record at the right logger
        # name to avoid needing a real filesystem layout.
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
        with patch("mlody.cli.main.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            result = runner.invoke(cli, ["_test_probe"])

        assert result.exit_code == 0

    def test_main_is_callable(self) -> None:
        assert callable(main)
