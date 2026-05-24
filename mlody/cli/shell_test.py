"""Tests for mlody.cli.shell — REPL namespace construction and shell subcommand."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from click.testing import CliRunner

from mlody.cli.main import cli
from mlody.cli.shell import (
    _build_repl_namespace,
    _configure_result_pretty_printer,
    _format_shell_result,
    _format_shell_result_ansi,
    _get_history_path,
    _launch_repl,
)
from mlody.common.struct import Struct


# ---------------------------------------------------------------------------
# _build_repl_namespace
# ---------------------------------------------------------------------------


class TestBuildReplNamespace:
    """Requirement: REPL namespace exposes show and workspace."""

    def test_namespace_contains_show_and_workspace(self, tmp_path: Path) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws, monorepo_root=tmp_path)

        assert set(namespace.keys()) == {"show", "workspace"}

    def test_workspace_in_namespace_is_same_object(self, tmp_path: Path) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws, monorepo_root=tmp_path)

        assert namespace["workspace"] is ws

    def test_show_delegates_to_show_fn(self, tmp_path: Path) -> None:
        # show() in the namespace delegates to show_fn with the monorepo_root
        ws = MagicMock()
        namespace = _build_repl_namespace(ws, monorepo_root=tmp_path)

        with patch("mlody.cli.shell.show_fn") as mock_show_fn:
            mock_show_fn.return_value = 0.001
            result = namespace["show"]("@bert//:lr")

        assert result == 0.001
        mock_show_fn.assert_called_once_with(
            "@bert//:lr",
            monorepo_root=tmp_path,
            workspace_root=None,
            full_workspace=False,
        )

    def test_show_resolves_multiple_targets_returns_list(self, tmp_path: Path) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws, monorepo_root=tmp_path)

        with patch("mlody.cli.shell.show_fn") as mock_show_fn:
            mock_show_fn.side_effect = [0.001, "adam"]
            result = namespace["show"]("@bert//:lr", "@bert//:optimizer")

        assert result == [0.001, "adam"]

    def test_show_propagates_exceptions(self, tmp_path: Path) -> None:
        ws = MagicMock()
        namespace = _build_repl_namespace(ws, monorepo_root=tmp_path)

        with patch("mlody.cli.shell.show_fn", side_effect=KeyError("NONEXISTENT")):
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
        ws.registry_view.host_session_globals.side_effect = (
            lambda _path, *, initial_globals=None: dict(initial_globals or {})
        )

        with patch("mlody.cli.shell._launch_repl") as mock_launch, patch(
            "mlody.cli.shell._get_history_path"
        ) as mock_hist:
            mock_hist.return_value = tmp_path / "repl_history"
            runner = CliRunner()
            result = runner.invoke(
                cli, ["shell"], obj={"workspace": ws, "verbose": False, "monorepo_root": tmp_path}
            )

        assert result.exit_code == 0
        mock_launch.assert_called_once()
        call_namespace, call_history = mock_launch.call_args.args
        assert set(call_namespace.keys()) == {"show", "workspace"}
        assert call_namespace["workspace"] is ws
        assert call_history == tmp_path / "repl_history"

    def test_launch_repl_passes_same_globals_and_locals(self, tmp_path: Path) -> None:
        embed_mock = MagicMock()
        fake_repl_module = SimpleNamespace(embed=embed_mock)

        with patch.dict(sys.modules, {"ptpython.repl": fake_repl_module}):
            _launch_repl({"answer": 42}, tmp_path / "repl_history")

        embed_mock.assert_called_once()
        assert embed_mock.call_args.kwargs == {
            "globals": {"answer": 42},
            "locals": {"answer": 42},
            "configure": ANY,
            "history_filename": str(tmp_path / "repl_history"),
            "title": "mlody shell",
        }
        assert callable(embed_mock.call_args.kwargs["configure"])

    def test_format_shell_result_normalizes_dataclass_wrappers(self) -> None:
        @dataclass(frozen=True)
        class Inner:
            value: int

        @dataclass(frozen=True)
        class Outer:
            inner: Inner
            child: object

        rendered = _format_shell_result(
            {"outer": Outer(inner=Inner(7), child=Struct(name="node"))},
            max_width=20,
        )

        assert "Outer(" not in rendered
        assert "Inner(" not in rendered
        assert "struct(" not in rendered
        assert "'outer'" in rendered
        assert "'child'" in rendered
        assert "\n" in rendered

    def test_format_shell_result_pretty_prints_struct_values(self) -> None:
        rendered = _format_shell_result(
            Struct(
                name="node",
                child=Struct(
                    name="leaf",
                    values=[1, 2, 3],
                ),
            ),
            max_width=20,
        )

        assert "struct(" not in rendered
        assert "'name': 'node'" in rendered
        assert "'values':" in rendered
        assert "1" in rendered and "2" in rendered and "3" in rendered
        assert "\n" in rendered

    def test_format_shell_result_limits_depth(self) -> None:
        rendered = _format_shell_result(
            {
                "level1": {
                    "level2": {
                        "level3": {
                            "level4": {
                                "leaf": "too-deep",
                            }
                        }
                    }
                }
            },
            max_width=40,
            max_depth=3,
        )

        assert "'level1'" in rendered
        assert "'level2'" in rendered
        assert "'level3'" in rendered
        assert "'level4'" not in rendered
        assert "..." in rendered

    def test_format_shell_result_ansi_uses_rich_styling(self) -> None:
        rendered = _format_shell_result_ansi({"name": "node"}, max_width=40)
        formatted = rendered.__pt_formatted_text__()  # type: ignore[attr-defined]

        assert any(style for style, _text in formatted)
        assert "node" in "".join(text for _style, text in formatted)

    def test_format_shell_result_ansi_does_not_write_to_stdout(self) -> None:
        stdout = StringIO()

        with redirect_stdout(stdout):
            rendered = _format_shell_result_ansi({"name": "node"}, max_width=40)

        assert stdout.getvalue() == ""
        formatted = rendered.__pt_formatted_text__()  # type: ignore[attr-defined]
        assert "node" in "".join(text for _style, text in formatted)

    def test_configure_result_pretty_printer_formats_eval_results(self) -> None:
        original_show_result = MagicMock()
        printer = MagicMock()
        printer.output.get_size.return_value = SimpleNamespace(columns=80)
        repl = SimpleNamespace(
            _show_result=original_show_result,
            _get_output_printer=lambda: printer,
            get_output_prompt=lambda: "Out[1]: ",
            enable_pager=True,
        )

        _configure_result_pretty_printer(repl)
        repl._show_result({"entity": Struct(name="node", child=Struct(name="leaf"))})

        original_show_result.assert_not_called()
        printer.display_result.assert_called_once()
        call_kwargs = printer.display_result.call_args.kwargs
        assert call_kwargs["reformat"] is False
        assert call_kwargs["highlight"] is False
        assert call_kwargs["paginate"] is True
        rendered = call_kwargs["result"].__pt_repr__()
        formatted = rendered.__pt_formatted_text__()  # type: ignore[attr-defined]
        assert any(style for style, _text in formatted)
        assert "node" in "".join(text for _style, text in formatted)

    def test_configure_result_pretty_printer_falls_back_to_default_renderer(self) -> None:
        original_show_result = MagicMock()
        printer = MagicMock()
        printer.output.get_size.return_value = SimpleNamespace(columns=80)
        repl = SimpleNamespace(
            _show_result=original_show_result,
            _get_output_printer=lambda: printer,
            get_output_prompt=lambda: "Out[1]: ",
            enable_pager=False,
        )

        _configure_result_pretty_printer(repl)
        with patch("mlody.cli.shell._format_shell_result_ansi", side_effect=ValueError("boom")):
            result = object()
            repl._show_result(result)

        original_show_result.assert_called_once_with(result)
        printer.display_result.assert_not_called()

    def test_shell_uses_host_module_globals_for_prelude_exports(self, tmp_path: Path) -> None:
        prelude_path = tmp_path / "mlody" / "shell" / "prelude.mlody"
        prelude_path.parent.mkdir(parents=True)
        prelude_path.write_text("# test prelude\n", encoding="utf-8")

        registry_view = MagicMock()
        registry_view.host_session_globals.side_effect = (
            lambda _path, *, initial_globals=None: {"builtins": "safe", **dict(initial_globals or {})}
        )
        registry_view.host_module_globals.return_value = {"vector": "host-visible"}
        workspace = MagicMock()
        workspace.registry_view = registry_view

        with patch("mlody.cli.shell._launch_repl") as mock_launch, patch(
            "mlody.cli.shell._get_history_path"
        ) as mock_hist, patch("mlody.resolver.resolve_workspace", return_value=(workspace, None)):
            mock_hist.return_value = tmp_path / "repl_history"
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["shell"],
                obj={
                    "monorepo_root": tmp_path,
                    "workspace_root": tmp_path,
                    "verbose": False,
                },
            )

        assert result.exit_code == 0
        registry_view.host_session_globals.assert_called_once()
        call_args, call_kwargs = registry_view.host_session_globals.call_args
        assert call_args == (tmp_path / "__shell_session__.mlody",)
        assert set(call_kwargs["initial_globals"]) == {"show", "workspace"}
        registry_view.eval_file.assert_called_once_with(prelude_path)
        registry_view.host_module_globals.assert_called_once_with(prelude_path)
        registry_view.module_globals.assert_not_called()
        call_namespace, _call_history = mock_launch.call_args.args
        assert call_namespace["builtins"] == "safe"
        assert call_namespace["vector"] == "host-visible"

    def test_shell_merges_eval_file_exports_into_shared_session_globals(self, tmp_path: Path) -> None:
        eval_file = tmp_path / "extra.mlody"
        eval_file.write_text("# injected\n", encoding="utf-8")

        registry_view = MagicMock()
        registry_view.host_session_globals.side_effect = (
            lambda _path, *, initial_globals=None: dict(initial_globals or {})
        )
        registry_view.host_module_globals.side_effect = (
            lambda path: {path.name: f"from-{path.name}"}
        )
        workspace = MagicMock()
        workspace.registry_view = registry_view

        with patch("mlody.cli.shell._launch_repl") as mock_launch, patch(
            "mlody.cli.shell._get_history_path"
        ) as mock_hist, patch("mlody.resolver.resolve_workspace", return_value=(workspace, None)):
            mock_hist.return_value = tmp_path / "repl_history"
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["shell", "--load", str(eval_file)],
                obj={
                    "monorepo_root": tmp_path,
                    "workspace_root": tmp_path,
                    "verbose": False,
                },
            )

        assert result.exit_code == 0
        call_namespace, _call_history = mock_launch.call_args.args
        assert call_namespace["extra.mlody"] == "from-extra.mlody"

    def test_shell_appears_in_cli_help(self) -> None:
        """Requirement: main() entry point imports and invokes — shell registered."""
        # shell_test imports mlody.cli.shell which registers the @cli.command(),
        # so the subcommand is visible in help after this module is imported.
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "shell" in result.output
