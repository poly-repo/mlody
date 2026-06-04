"""Tests for mlody.cli.dump."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

import mlody.cli.dump  # noqa: F401
from mlody.cli.main import cli
from mlody.common.action import RegisteredAction
from mlody.common.struct import Struct
from mlody.common.task import RegisteredTask
from mlody.common.value import RegisteredValue
from mlody.resolver.errors import WorkspaceResolutionError


class TestDumpCommand:
    def _freshness(self, name: str = "always") -> Struct:
        return Struct(
            kind="freshness",
            type=name,
            name=name,
            attributes={},
            _allowed_attrs={},
            validator=lambda value: True,
            abstract=False,
            _root_kind=name,
        )

    def _location(self, location_type: str, **attributes: object) -> Struct:
        return Struct(
            kind="location",
            type=location_type,
            name=location_type,
            attributes=attributes,
            _allowed_attrs={key: "any" for key in attributes},
            validator=lambda value: True,
            abstract=False,
            _root_kind=location_type,
        )

    def _type(self, type_name: str, **attributes: object) -> Struct:
        return Struct(
            kind="type",
            type=type_name,
            name=type_name,
            attributes=attributes,
            _allowed_attrs={key: "any" for key in attributes},
            validator=lambda value: True,
            abstract=type_name == "nothing",
            _root_kind=type_name,
        )

    def _representation(self, name: str) -> Struct:
        return Struct(
            kind="representation",
            name=name,
            attributes={},
            _allowed_attrs={},
            _attrs_mandatory=[],
        )

    def _build_ref(self, build_type: str, target: str) -> Struct:
        return Struct(
            kind="build_ref",
            type=build_type,
            name=build_type,
            target=target,
            _allowed_attrs={},
        )

    def _value(
        self,
        *,
        name: str,
        type_value: object | None = None,
        location_value: object | None = None,
        freshness_value: object | None = None,
        default: object | None = None,
        representation: object | None = None,
        group: str | None = None,
    ) -> RegisteredValue:
        return RegisteredValue(
            Struct(
                kind="value",
                name=name,
                description="",
                type=type_value if type_value is not None else self._type("nothing"),
                location=(
                    location_value
                    if location_value is not None
                    else self._location("inline")
                ),
                freshness=(
                    freshness_value
                    if freshness_value is not None
                    else self._freshness()
                ),
                unit=None,
                default=default,
                source=None,
                representation=representation,
                group=group,
            )
        )

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

    def test_dump_compacts_registered_task_shapes(self) -> None:
        string_type = self._type("string")
        vector_string = self._type("vector", element_type=string_type)
        nothing_type = self._type("nothing")
        hf_model_type = self._type("hf-model")
        inline_location = self._location("inline")
        named_arg_location = self._location("named_arg")
        posix_location = self._location("posix", path=["{vendor}/{model}/{sha}"])
        json_representation = self._representation("json")

        action = RegisteredAction(
            Struct(
                kind="action",
                name="downloader-action",
                description="",
                inputs=[],
                outputs=[
                    self._value(
                        name="model",
                        type_value=hf_model_type,
                        location_value=posix_location,
                    ),
                    self._value(name="committoid"),
                    self._value(
                        name="releases",
                        type_value=vector_string,
                        location_value=inline_location,
                        representation=json_representation,
                    ),
                ],
                config=[
                    self._value(
                        name="vendor",
                        type_value=string_type,
                        location_value=named_arg_location,
                    ),
                    self._value(
                        name="model",
                        type_value=string_type,
                        location_value=named_arg_location,
                    ),
                    self._value(
                        name="sha",
                        type_value=string_type,
                        location_value=named_arg_location,
                    ),
                    self._value(
                        name="workers",
                        type_value=self._type("integer", min=1, max=8),
                        location_value=named_arg_location,
                        default=1,
                    ),
                ],
                requirements=[
                    Struct(kind="requirement", requirement="cpu", count=2, type="*")
                ],
                implementation=None,
                build=self._build_ref("bazel", ":model-download"),
            )
        )
        task = RegisteredTask(
            Struct(
                kind="task",
                name="downloader",
                description="",
                inputs=[],
                outputs=[
                    self._value(name="model", type_value=nothing_type, group="model"),
                    self._value(
                        name="committoid",
                        type_value=nothing_type,
                        group="model",
                    ),
                    self._value(
                        name="releases",
                        type_value=vector_string,
                        location_value=inline_location,
                        representation=json_representation,
                        group="info",
                    ),
                ],
                action=action,
                config=[],
                execution=None,
            )
        )
        fake_workspace = MagicMock()
        fake_workspace.registry_view.iter_registry_items.return_value = [
            (("task", "pkg/a", "downloader"), task)
        ]

        with patch(
            "mlody.cli.dump.resolve_workspace_raw",
            return_value=(fake_workspace, None),
        ):
            result = CliRunner().invoke(
                cli,
                ["dump", "--kind", "task", "--name", "downloader"],
                obj=self._context(),
            )

        assert result.exit_code == 0
        assert json.loads(result.output) == [
            {
                "kind": "task",
                "name": "downloader",
                "outputs": [
                    {
                        "kind": "value",
                        "name": "model",
                        "group": "model",
                        "type": "nothing",
                        "location": "inline",
                        "freshness": "always",
                    },
                    {
                        "kind": "value",
                        "name": "committoid",
                        "group": "model",
                        "type": "nothing",
                        "location": "inline",
                        "freshness": "always",
                    },
                    {
                        "kind": "value",
                        "name": "releases",
                        "group": "info",
                        "representation": "json",
                        "location": "inline",
                        "freshness": "always",
                        "type": {"type": "vector", "element_type": "string"},
                    },
                ],
                "action": {
                    "kind": "action",
                    "name": "downloader-action",
                    "outputs": [
                        {
                            "kind": "value",
                            "name": "model",
                            "type": "hf-model",
                            "freshness": "always",
                            "location": {
                                "type": "posix",
                                "path": "{vendor}/{model}/{sha}",
                            },
                        },
                        {
                            "kind": "value",
                            "name": "committoid",
                            "type": "nothing",
                            "location": "inline",
                            "freshness": "always",
                        },
                        {
                            "kind": "value",
                            "name": "releases",
                            "representation": "json",
                            "location": "inline",
                            "freshness": "always",
                            "type": {"type": "vector", "element_type": "string"},
                        },
                    ],
                    "config": [
                        {
                            "kind": "value",
                            "name": "vendor",
                            "type": "string",
                            "freshness": "always",
                            "location": "named_arg",
                        },
                        {
                            "kind": "value",
                            "name": "model",
                            "type": "string",
                            "freshness": "always",
                            "location": "named_arg",
                        },
                        {
                            "kind": "value",
                            "name": "sha",
                            "type": "string",
                            "freshness": "always",
                            "location": "named_arg",
                        },
                        {
                            "kind": "value",
                            "name": "workers",
                            "default": 1,
                            "freshness": "always",
                            "type": {"type": "integer", "min": 1, "max": 8},
                            "location": "named_arg",
                        },
                    ],
                    "build": {"type": "bazel", "target": ":model-download"},
                    "requirements": [{"requirement": "cpu", "count": 2}],
                },
            }
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
