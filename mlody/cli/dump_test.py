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
                {"path": Path("/tmp/raw"), "callable": lambda: None},
            ),
            (
                ("task", "pkg/a", "a"),
                {"kind": "task", "nested": {"epochs": 3}},
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
        assert json.loads(result.output) == {
            "all": [
                {
                    "kind": "task",
                    "name": "a",
                    "stem": "pkg/a",
                    "value": {"kind": "task", "nested": {"epochs": 3}},
                },
                {
                    "kind": "value",
                    "name": "z",
                    "stem": "pkg/a",
                    "value": {"callable": "<callable>", "path": "/tmp/raw"},
                },
            ]
        }

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
        assert json.loads(result.output) == {"all": []}

    def test_dump_surfaces_workspace_resolution_errors(self) -> None:
        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            side_effect=WorkspaceResolutionError("bad ref"),
        ):
            result = CliRunner().invoke(cli, ["dump"], obj=self._context())

        assert result.exit_code == 1
        assert "Error: bad ref" in result.output
