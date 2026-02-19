"""Tests for mlody.cli.shell — REPL namespace construction and shell subcommand."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mlody.cli.main import cli
from mlody.cli.shell import _build_repl_namespace, _get_history_path


# ---------------------------------------------------------------------------
# _build_repl_namespace
# ---------------------------------------------------------------------------


class TestBuildReplNamespace:
    """Requirement: REPL namespace exposes show and workspace."""

    def test_namespace_contains_show_and_workspace(self) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws)

        assert set(namespace.keys()) == {"show", "workspace"}

    def test_workspace_in_namespace_is_same_object(self) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws)

        assert namespace["workspace"] is ws

    def test_show_resolves_single_target(self) -> None:
        # show() in the namespace must delegate to show_fn(workspace, target),
        # so callers don't need to know about the workspace at all.
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        namespace = _build_repl_namespace(ws)

        result = namespace["show"]("@bert//:lr")

        assert result == 0.001
        ws.resolve.assert_called_once_with("@bert//:lr")

    def test_show_resolves_multiple_targets_returns_list(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, "adam"]
        namespace = _build_repl_namespace(ws)

        result = namespace["show"]("@bert//:lr", "@bert//:optimizer")

        assert result == [0.001, "adam"]

    def test_show_raises_key_error_on_unknown_target(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = KeyError("NONEXISTENT")
        namespace = _build_repl_namespace(ws)

        with pytest.raises(KeyError, match="NONEXISTENT"):
            namespace["show"]("@NONEXISTENT//:x")


# ---------------------------------------------------------------------------
# _get_history_path
# ---------------------------------------------------------------------------


class TestGetHistoryPath:
    """Requirement: REPL history is persisted across sessions."""

    def test_returns_xdg_data_path(self, tmp_path: Path) -> None:
        with patch("mlody.cli.shell.Path") as mock_path_cls:
            # Simulate Path.home() → tmp_path, Path / ... works normally
            mock_path_cls.home.return_value = tmp_path
            # Make path joining work by delegating to real Path
            mock_path_cls.side_effect = Path

            result = _get_history_path()

        expected = tmp_path / ".local" / "share" / "mlody" / "repl_history"
        assert result == expected

    def test_creates_parent_directory_if_missing(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        expected_dir = fake_home / ".local" / "share" / "mlody"

        with patch("mlody.cli.shell.Path") as mock_path_cls:
            mock_path_cls.home.return_value = fake_home
            mock_path_cls.side_effect = Path

            _get_history_path()

        assert expected_dir.is_dir()

    def test_does_not_raise_when_mkdir_fails(self, tmp_path: Path) -> None:
        # Permission failures must not prevent REPL launch — graceful degradation.
        with patch("mlody.cli.shell.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            mock_path_cls.side_effect = Path

            with patch.object(Path, "mkdir", side_effect=OSError("permission denied")):
                # Must not raise
                _get_history_path()


# ---------------------------------------------------------------------------
# shell command
# ---------------------------------------------------------------------------


class TestShellCommand:
    """Requirement: Shell subcommand launches ptpython REPL."""

    def test_shell_invokes_launch_repl_with_correct_namespace(self, tmp_path: Path) -> None:
        # _launch_repl is the test seam — mocking it avoids starting an
        # interactive process while still verifying the wiring is correct.
        ws = MagicMock()

        with patch("mlody.cli.shell._launch_repl") as mock_launch, patch(
            "mlody.cli.shell._get_history_path"
        ) as mock_hist:
            mock_hist.return_value = tmp_path / "repl_history"
            runner = CliRunner()
            result = runner.invoke(cli, ["shell"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        mock_launch.assert_called_once()
        call_namespace, call_history = mock_launch.call_args.args
        assert set(call_namespace.keys()) == {"show", "workspace"}
        assert call_namespace["workspace"] is ws
        assert call_history == tmp_path / "repl_history"

    def test_shell_appears_in_cli_help(self) -> None:
        """Requirement: main() entry point imports and invokes — shell registered."""
        # shell_test imports mlody.cli.shell which registers the @cli.command(),
        # so the subcommand is visible in help after this module is imported.
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "shell" in result.output
