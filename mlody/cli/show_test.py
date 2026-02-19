"""Tests for mlody.cli.show — show subcommand and show_fn."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from common.python.starlarkish.core.struct import struct

from mlody.cli.main import cli
from mlody.cli.show import show_fn


# ---------------------------------------------------------------------------
# show_fn — functional form
# ---------------------------------------------------------------------------


class TestShowFn:
    """Requirement: Functional form for shell REPL."""

    def test_single_target_returns_value(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        result = show_fn(ws, "@bert//:lr")
        assert result == 0.001
        ws.resolve.assert_called_once_with("@bert//:lr")

    def test_multiple_targets_returns_list(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, "adam"]
        result = show_fn(ws, "@bert//:lr", "@bert//:optimizer")
        assert result == [0.001, "adam"]

    def test_error_propagation_key_error(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = KeyError("NONEXISTENT")
        with pytest.raises(KeyError, match="NONEXISTENT"):
            show_fn(ws, "@NONEXISTENT//:x")

    def test_error_propagation_attribute_error(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = AttributeError("no_field")
        with pytest.raises(AttributeError, match="no_field"):
            show_fn(ws, "@bert//:no_field")


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
