"""Tests for mlody.cli.dump."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

import mlody.cli.dump  # noqa: F401
from mlody.cli.main import cli
from mlody.resolver.errors import WorkspaceResolutionError


class TestDumpCommand:
    def _context(self) -> dict[str, object]:
        monorepo_root = Path("/repo")
        return {
            "monorepo_root": monorepo_root,
            "workspace_root": monorepo_root / "mlody",
            "roots": Path("mlody/custom-roots.mlody"),
            "verbose": False,
            "full_workspace": True,
        }

    def test_dump_without_target_serializes_raw_registry_rows(self) -> None:
        fake_workspace = MagicMock()
        fake_workspace.registry_view.iter_registry_items.return_value = [
            (
                ("value", "pkg/a", "z"),
                {
                    "name": "z",
                    "path": Path("/tmp/raw"),
                    "callable": lambda: None,
                    "lineage": {"ignored": True},
                    "raw": '{"ignored": true}',
                    "_source_range": {"start_line": 9},
                },
            ),
            (
                ("task", "pkg/a", "a"),
                {
                    "kind": "task",
                    "name": "a",
                    "nested": {"epochs": 3},
                    "methods": [{"name": "ignored"}],
                },
            ),
        ]

        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            return_value=(fake_workspace, None),
        ) as mock_resolve:
            result = CliRunner().invoke(cli, ["dump"], obj=self._context())

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with(
            None,
            monorepo_root=Path("/repo"),
            workspace_root=Path("/repo/mlody"),
            roots_file=Path("mlody/custom-roots.mlody"),
            full_workspace=False,
            print_fn=mlody.cli.dump.click.echo,
            verbose=False,
        )
        assert json.loads(result.output) == [
            {"kind": "task", "name": "a", "nested": {"epochs": 3}},
            {"callable": "<callable>", "kind": "value", "name": "z", "path": "/tmp/raw"},
        ]

    def test_dump_filters_rows_by_kind_and_name(self) -> None:
        fake_workspace = MagicMock()
        fake_workspace.registry_view.iter_registry_items.return_value = [
            (("task", "pkg/a", "alpha"), {"kind": "task", "name": "alpha"}),
            (("value", "pkg/a", "alpha"), {"kind": "value", "name": "alpha"}),
            (("task", "pkg/a", "beta"), {"kind": "task", "name": "beta"}),
        ]

        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            return_value=(fake_workspace, None),
        ):
            kind_result = CliRunner().invoke(
                cli,
                ["dump", "--kind", "task"],
                obj=self._context(),
            )
            name_result = CliRunner().invoke(
                cli,
                ["dump", "--name", "alpha"],
                obj=self._context(),
            )
            combined_result = CliRunner().invoke(
                cli,
                ["dump", "--kind", "task", "--name", "alpha"],
                obj=self._context(),
            )

        assert kind_result.exit_code == 0
        assert json.loads(kind_result.output) == [
            {"kind": "task", "name": "alpha"},
            {"kind": "task", "name": "beta"},
        ]

        assert name_result.exit_code == 0
        assert json.loads(name_result.output) == [
            {"kind": "task", "name": "alpha"},
            {"kind": "value", "name": "alpha"},
        ]

        assert combined_result.exit_code == 0
        assert json.loads(combined_result.output) == [{"kind": "task", "name": "alpha"}]

    def test_dump_with_target_uses_show_style_workspace_selection(self) -> None:
        fake_workspace = MagicMock()
        fake_workspace.registry_view.iter_registry_items.return_value = []

        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            return_value=(fake_workspace, "a" * 40),
        ) as mock_resolve:
            result = CliRunner().invoke(
                cli,
                ["dump", "main|@bert//models:lr"],
                obj=self._context(),
            )

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with(
            "main|@bert//models:lr",
            monorepo_root=Path("/repo"),
            workspace_root=Path("/repo/mlody"),
            roots_file=Path("mlody/custom-roots.mlody"),
            full_workspace=False,
            print_fn=mlody.cli.dump.click.echo,
            verbose=False,
        )
        assert json.loads(result.output) == []

    def test_dump_surfaces_workspace_resolution_errors(self) -> None:
        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            side_effect=WorkspaceResolutionError("bad ref"),
        ):
            result = CliRunner().invoke(cli, ["dump"], obj=self._context())

        assert result.exit_code == 1
        assert "Error: bad ref" in result.output
