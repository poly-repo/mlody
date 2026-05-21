"""Tests for persistent mlody CLI server mode."""

from __future__ import annotations

from datetime import datetime, timezone
import functools
import http.server
import socket
import json
import logging
import os
import threading
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import networkx
import pytest
import click

from common.python.starlarkish.core.struct import Struct
from mlody.cli.autocomplete import StageAutocompleteRequest
from mlody.cli.server import (
    MlodyApiRequestHandler,
    ServerConfig,
    ServerCommandRequest,
    _history_prompt_and_breadcrumb,
    _spawn_restart_watcher,
    _stage_json_data,
    collect_command_response,
    create_http_server,
    execute_stage_autocomplete_response,
    execute_stage_command_response,
    execute_verbatim_command_response,
    parse_command_request,
    parse_verbatim_command_request,
)
from mlody.core.action_graph import MlodyActionGraphNode
from mlody.core.action_graph_value import MlodyActionGraphType
from mlody.core.dag import Edge, TaskNode, ValueNode
from mlody.core.dag_value import MlodyDagType
from mlody.core.workspace_models import RootInfo
from mlody.resolver import (
    MlodyActionValue,
    MlodyFolderValue,
    MlodyTaskValue,
    MlodyValueValue,
    MlodyVectorValue,
)
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.structural import MlodySourceRangeValue


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    """Serve *tmp_path* over HTTP and return ``(base_url, root)``."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", tmp_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _server_config(tmp_path: Path, *, http_port: int = 0) -> ServerConfig:
    return ServerConfig(
        monorepo_root=tmp_path,
        workspace_root=tmp_path,
        roots=None,
        verbose=False,
        full_workspace=False,
        http_host="127.0.0.1",
        http_port=http_port,
        lsp_host="127.0.0.1",
        lsp_port=8766,
    )


class _FakeRegistryView:
    def __init__(self, items: list[tuple[tuple[object, object, object], object]]) -> None:
        self._items = tuple(items)

    def iter_registry_items(self) -> tuple[tuple[tuple[object, object, object], object], ...]:
        return self._items


def _autocomplete_workspace(tmp_path: Path) -> tuple[SimpleNamespace, Path]:
    workspace_root = tmp_path / "sandboxes" / "exp1"
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = SimpleNamespace(
        root_infos={
            "common": RootInfo(
                name="common",
                path="//mlody/common",
                description="shared",
            ),
            "pixelle": RootInfo(
                name="pixelle",
                path="//mlody/teams/pixelle",
                description="vision",
            ),
        },
        registry_view=_FakeRegistryView(
            [
                (("task", "sandboxes/exp1/projects", "omega"), object()),
                (("task", "sandboxes/exp1/projects", "orbit"), object()),
                (("task", "sandboxes/exp1/folders/reports", "sales"), object()),
                (("type", "mlody/teams/pixelle/datasets", "celebA"), object()),
                (("task", "mlody/teams/pixelle/datasets", "celebA-dataset"), object()),
                (("task", "mlody/teams/pixelle/datasets", "imagenet"), object()),
            ]
        ),
        _monorepo_root=tmp_path,
        _workspace_root=workspace_root,
    )
    return workspace, workspace_root


class TestParseCommandRequest:
    def test_uses_targets_and_preserves_options(self) -> None:
        request = parse_command_request(
            {
                "requestId": "req-1",
                "command": "show",
                "targets": ["@root//projects:artifacts"],
                "options": {"runAs": "maya", "config": ["foo=1"]},
            }
        )

        assert request.request_id == "req-1"
        assert request.command == "show"
        assert request.arguments == ("@root//projects:artifacts",)
        assert request.options["runAs"] == "maya"
        assert request.options["config"] == ["foo=1"]

    def test_preserves_workspace_root_alias(self) -> None:
        request = parse_command_request(
            {
                "command": "show",
                "targets": ["@root//projects:artifacts"],
                "workspaceRoot": "/tmp/workspace-a",
            }
        )

        assert request.options["workspaceRoot"] == "/tmp/workspace-a"

    def test_falls_back_to_shell_split_input(self) -> None:
        request = parse_command_request(
            {
                "command": "show",
                "input": "@root//projects:artifacts metrics/summary",
            }
        )

        assert request.command == "show"
        assert request.arguments == (
            "@root//projects:artifacts",
            "metrics/summary",
        )

    def test_rejects_show_without_targets(self) -> None:
        with pytest.raises(ValueError, match="Show requests require at least one target"):
            parse_command_request({"command": "show", "input": ""})

    def test_verbatim_request_preserves_raw_show_target(self) -> None:
        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
            }
        )

        assert request.command == "show"
        assert request.arguments == (
            "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
        )


class TestCollectCommandResponse:
    def test_show_response_serializes_results(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = None

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.parse_label",
            lambda _label: object(),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyFolderValue(
                path="artifacts",
                children=["config", "plots"],
            ),
        )

        request = parse_command_request(
            {
                "requestId": "req-show",
                "command": "show",
                "targets": ["@root//projects:artifacts"],
                "options": {"runAs": "maya"},
            }
        )
        response = collect_command_response(_server_config(tmp_path), request)

        assert response["status"] == "done"
        assert len(response["errors"]) == 0
        assert len(response["results"]) == 1

        result = response["results"][0]
        assert result["event"] == "result"
        assert result["resolvedSha"] == "sha123"
        assert result["user"] == "maya"
        assert result["value"]["kind"] == "folder"
        assert result["value"]["payload"] == {
            "path": "artifacts",
            "children": ["config", "plots"],
        }

    def test_unsupported_command_returns_error(self, tmp_path: Path) -> None:
        request = parse_command_request({"command": "system", "input": "status"})
        response = collect_command_response(_server_config(tmp_path), request)

        assert response["status"] == "error"
        assert response["results"] == []
        assert len(response["errors"]) == 1
        assert response["errors"][0]["message"] == "Unsupported command: system"


class TestStageAutocompleteResponse:
    def test_empty_breadcrumb_and_prompt_returns_no_completions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=(),
                prompt="",
            ),
        )

        assert response == {"completions": [], "additionalData": {}}

    def test_root_completion_returns_matching_root_names(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=(),
                prompt="@p",
            ),
        )

        assert response["completions"] == [{"label": "pixelle", "kind": "root"}]

    def test_rootless_package_completion_returns_folder_kind(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("//",),
                prompt="fo",
            ),
        )

        assert response["completions"] == [{"label": "folders", "kind": "folder"}]

    def test_package_completion_returns_source_file_kind(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("//",),
                prompt="pr",
            ),
        )

        assert response["completions"] == [
            {"label": "projects", "kind": "source_file"},
        ]

    def test_target_completion_returns_matching_entity_names(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("//", "projects:"),
                prompt="o",
            ),
        )

        assert response["completions"] == [
            {"label": "omega", "kind": "entity"},
            {"label": "orbit", "kind": "entity"},
        ]

    def test_target_completion_omits_type_only_entities(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("@pixelle", "//", "datasets:"),
                prompt="ce",
            ),
        )

        assert response["completions"] == [
            {"label": "celebA-dataset", "kind": "entity"},
        ]

    def test_field_completion_resolves_parent_label_and_lists_immediate_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )
        captured: dict[str, object] = {}

        def _fake_resolve(label, _workspace):
            captured["label"] = label.format_inner()
            return MlodyValueValue(
                struct={
                    "name": "omega",
                    "namespace": "projects",
                    "notes": "ready",
                }
            )

        monkeypatch.setattr(
            "mlody.cli.autocomplete.resolve_label_to_value",
            _fake_resolve,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("//", "projects:", "omega"),
                prompt=".na",
            ),
        )

        assert captured["label"] == "//projects:omega"
        assert response["completions"] == [
            {"label": "name", "kind": "field"},
            {"label": "namespace", "kind": "field"},
        ]

    def test_unsupported_syntax_returns_no_completions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace, workspace_root = _autocomplete_workspace(tmp_path)
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, _workspace_root: workspace,
        )

        response = execute_stage_autocomplete_response(
            _server_config(tmp_path),
            StageAutocompleteRequest(
                workspace_root="sandboxes/exp1",
                breadcrumb=("//", "projects:", "omega"),
                prompt="|next",
            ),
        )

        assert response == {"completions": [], "additionalData": {}}


class TestExecuteVerbatimCommandResponse:
    def test_invokes_real_show_command_without_workspace_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_invoke(self, cli, args, **kwargs):
            captured["args"] = list(args)
            captured["obj"] = kwargs.get("obj")
            captured["color"] = kwargs.get("color")
            captured["catch_exceptions"] = kwargs.get("catch_exceptions")
            return SimpleNamespace(exit_code=0, output="Value for user 'mav'\nrow 1\n")

        monkeypatch.setattr("click.testing.CliRunner.invoke", _fake_invoke)

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                "options": {"runAs": "agarcia"},
            }
        )
        response = execute_verbatim_command_response(_server_config(tmp_path), request)

        assert captured["args"] == [
            "show",
            "--as",
            "agarcia",
            "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
        ]
        assert captured["obj"] == {
            "monorepo_root": tmp_path,
            "roots": None,
        }
        assert captured["color"] is False
        assert captured["catch_exceptions"] is False
        assert response["status"] == "done"
        assert response["output"] == "Value for user 'mav'\nrow 1\n"

    def test_invokes_real_show_command_with_workspace_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}
        workspace_root = tmp_path / "sandboxes" / "exp1"
        workspace_root.mkdir(parents=True)

        def _fake_invoke(self, cli, args, **kwargs):
            captured["args"] = list(args)
            return SimpleNamespace(exit_code=0, output="ok\n")

        monkeypatch.setattr("click.testing.CliRunner.invoke", _fake_invoke)

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                "options": {
                    "runAs": "agarcia",
                    "workspaceRoot": "sandboxes/exp1",
                },
            }
        )
        execute_verbatim_command_response(_server_config(tmp_path), request)

        assert captured["args"] == [
            "--workspace",
            str(workspace_root.resolve()),
            "show",
            "--as",
            "agarcia",
            "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
        ]


class TestExecuteStageCommandResponse:
    def test_resolves_show_into_stage_json_without_workspace_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        def _fake_resolve_workspace(target: str, **kwargs):
            captured["target"] = target
            captured["workspace_root"] = kwargs.get("workspace_root")
            captured["user"] = kwargs.get("user")
            return _FakeWorkspace(), "sha123"

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            _fake_resolve_workspace,
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyFolderValue(
                path="artifacts",
                children=["config", "plots"],
            ),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                "options": {"runAs": "agarcia"},
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert captured["target"] == "@pixelle//datasets:celebA-dataset.train[@sql limit 2]"
        assert captured["workspace_root"] == tmp_path
        assert captured["user"] == "agarcia"
        assert response["requestId"] == request.request_id
        assert response["kind"] == "result"
        assert response["view"]["type"] == "json"
        assert response["data"]["kind"] == "folder"
        assert response["data"]["payload"]["path"] == "artifacts"

    def test_resolves_show_into_selected_workspace_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}
        workspace_root = tmp_path / "sandboxes" / "exp1"
        workspace_root.mkdir(parents=True)

        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        def _fake_resolve_workspace(target: str, **kwargs):
            captured["target"] = target
            captured["workspace_root"] = kwargs.get("workspace_root")
            return _FakeWorkspace(), "sha123"

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            _fake_resolve_workspace,
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyFolderValue(
                path="artifacts",
                children=["config", "plots"],
            ),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                "options": {"workspaceRoot": "sandboxes/exp1"},
            }
        )
        execute_stage_command_response(_server_config(tmp_path), request)

        assert captured["target"] == "@pixelle//datasets:celebA-dataset.train[@sql limit 2]"
        assert captured["workspace_root"] == workspace_root.resolve()

    def test_serializes_raw_tabular_results_as_stage_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: _RawAttrValue(
                value=[
                    {"name": "Ada", "salary": 120000},
                    {"name": "Grace", "salary": 135000},
                ],
                label=_label,
            ),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "table"
        assert response["view"]["columns"] == [
            {"key": "name", "label": "name"},
            {"key": "salary", "label": "salary"},
        ]
        assert response["data"] == [
            {"name": "Ada", "salary": 120000},
            {"name": "Grace", "salary": 135000},
        ]

    def test_serializes_tasks_as_dedicated_stage_task_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        task_struct = Struct(
            kind="task",
            name="train",
            description="Train the ranking model",
            inputs=[
                Struct(
                    kind="value",
                    name="dataset",
                    description="Prepared training dataset",
                    type=Struct(kind="type", type="string", name="string"),
                )
            ],
            outputs=[
                Struct(
                    kind="value",
                    name="model",
                    description="Serialized model artifact",
                    type=Struct(kind="type", type="string", name="string"),
                )
            ],
            config=[
                Struct(
                    kind="value",
                    name="epochs",
                    description="Training epochs",
                    type=Struct(kind="type", type="integer", name="integer"),
                )
            ],
            action=Struct(
                kind="action",
                name="fit_model",
                implementation=Struct(
                    kind="implementation",
                    type="container",
                    name="container",
                    build=Struct(
                        kind="build_ref",
                        type="bazel",
                        name="bazel",
                        target="//mlody/train:image",
                    ),
                ),
            ),
            execution=Struct(
                kind="execution",
                type="kubernetes",
                name="kubernetes",
                namespace="mlody",
                service_account="trainer",
            ),
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyTaskValue(struct=task_struct),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//mlody/train:train",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "task"
        assert response["data"]["name"] == "train"
        assert response["data"]["description"] == "Train the ranking model"
        assert response["data"]["inputs"] == [
            {
                "name": "dataset",
                "type": "string",
                "description": "Prepared training dataset",
                "details": [],
                "detailsText": "",
            }
        ]
        assert response["data"]["sections"][0]["label"] == "Inputs"
        assert response["data"]["sections"][0]["values"] == response["data"]["inputs"]
        assert response["data"]["attributes"][1]["value"] == "container"
        assert response["data"]["attributes"][1]["detailsText"] == (
            "build=bazel(target=//mlody/train:image)"
        )
        assert response["data"]["attributes"][2]["value"] == "kubernetes"
        assert response["data"]["attributes"][2]["detailsText"] == (
            "namespace=mlody, service_account=trainer"
        )

    def test_renders_action_summary_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        action_struct = Struct(
            kind="action",
            name="fit_model",
            description="Fit a ranking model",
            inputs=[
                Struct(
                    kind="value",
                    name="dataset",
                    description="Prepared training dataset",
                    type=Struct(kind="type", type="string", name="string"),
                )
            ],
            outputs=[
                Struct(
                    kind="value",
                    name="model",
                    description="Fitted ranking model",
                    type=Struct(kind="type", type="record", name="ranking-model"),
                )
            ],
            config=[
                Struct(
                    kind="value",
                    name="epochs",
                    description="Training epochs",
                    type=Struct(kind="type", type="integer", name="integer"),
                )
            ],
            implementation=Struct(
                kind="implementation",
                type="container",
                name="container",
                build=Struct(
                    kind="build_ref",
                    type="bazel",
                    name="bazel",
                    target="//mlody/train:image",
                ),
            ),
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyActionValue(struct=action_struct),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//mlody/train:fit_model",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "action"
        assert response["data"]["name"] == "fit_model"
        assert response["data"]["sections"][0]["values"][0]["name"] == "dataset"
        assert response["data"]["sections"][1]["values"][0]["type"] == "ranking-model"
        assert response["data"]["attributes"][0]["value"] == "container"

    def test_renders_multi_result_stage_list_for_action_vectors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        first_action = Struct(
            kind="action",
            name="downloader-action-info",
            outputs=[],
            config=[],
            implementation=Struct(kind="implementation", type="sandbox", name="sandbox"),
        )
        second_action = Struct(
            kind="action",
            name="downloader-action",
            outputs=[],
            config=[],
            implementation=Struct(kind="implementation", type="sandbox", name="sandbox"),
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyVectorValue(
                elements=(
                    MlodyActionValue(struct=first_action),
                    MlodyActionValue(struct=second_action),
                )
            ),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@common//huggingface/downloader:downloader.action",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "result-list"
        assert response["view"]["rowCount"] == 2
        assert isinstance(response["data"], list)
        assert response["data"][0]["view"]["type"] == "action"
        assert response["data"][0]["view"].get("title", "") == ""
        assert response["data"][0]["data"]["name"] == "downloader-action-info"
        assert response["data"][1]["view"].get("title", "") == ""
        assert response["data"][1]["data"]["name"] == "downloader-action"

    def test_falls_back_to_json_when_value_has_no_tabular_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="a-string",
                    data=("FOOBAR",),
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server.source_from_value",
            lambda _value: None,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//simple:a-string",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "json"
        assert response["view"]["title"] == "//simple:a-string"
        assert response["data"] == {
            "kind": "value",
            "name": "a-string",
            "data": ["FOOBAR"],
        }
        assert "valueType" not in response

    def test_serializes_non_tabular_remote_values_as_asset_metadata_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        http_server: tuple[str, Path],
    ) -> None:
        base_url, root = http_server
        (root / "data.json").write_text('{"hello": "world"}', encoding="utf-8")

        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="remote-meta",
                    type=None,
                    location=Struct(
                        kind="location",
                        type="remote",
                        name="remote",
                        attributes={"uri": f"{base_url}/data.json"},
                    ),
                    freshness=Struct(
                        kind="freshness",
                        type="manual",
                        name="manual",
                        attributes={},
                    ),
                    default=None,
                    source=None,
                    representation=Struct(
                        kind="representation",
                        name="json",
                        attributes={},
                    ),
                    _lineage=[],
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server.source_from_value",
            lambda _value: None,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//simple:remote-meta",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "json"
        assert response["data"]["kind"] == "asset"
        assert response["data"]["origin"] == "remote"
        assert response["data"]["representation"] == "json"
        assert response["data"]["freshness"] == "manual"
        assert response["data"]["uri"] == f"{base_url}/data.json"
        assert response["data"]["path"].endswith(".json")
        assert response["data"]["contentHash"]
        assert "valueType" not in response

    def test_typed_scalar_json_result_includes_value_type_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        string_type = Struct(
            kind="type",
            name="string",
            type="string",
            _root_kind="string",
            attributes={},
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="_hash",
                    type=string_type,
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._display_payload",
            lambda _value: "019e27e2-5acc-72a2-8a0c-d56295a4e0fa",
        )
        monkeypatch.setattr(
            "mlody.cli.server.source_from_value",
            lambda _value: None,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "@common//huggingface/downloader:downloader._hash",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"]["type"] == "json"
        assert response["data"] == "019e27e2-5acc-72a2-8a0c-d56295a4e0fa"
        assert response["valueType"] == {
            "kind": "type",
            "name": "string",
            "type": "string",
            "_root_kind": "string",
            "attributes": {},
        }

    def test_serializes_lineage_values_as_stage_lineage_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        lineage_type = Struct(
            kind="type",
            name="vector",
            type="vector",
            _root_kind="vector",
            attributes={
                "element_type": Struct(
                    kind="type",
                    name="mlody-lineage-event",
                    type="mlody-lineage-event",
                    type_name="mlody-lineage-event",
                    _root_kind="record",
                    attributes={},
                )
            },
        )
        lineage_events = [
            Struct(
                kind="lineage_event",
                source="DEFAULT: foo",
                new_value=Struct(kind="location", data="foo"),
            ),
            Struct(
                kind="lineage_event",
                source="COMMAND_LINE: //simple:a-string=bar",
                new_value=Struct(kind="location", data="bar"),
            ),
        ]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="lineage",
                    type=lineage_type,
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._display_payload",
            lambda _value: lineage_events,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//simple:a-string.lineage",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"] == {
            "type": "lineage",
            "title": "//simple:a-string.lineage",
            "rowCount": 2,
        }
        assert response["data"] == [
            {
                "source": "default",
                "value": "foo",
                "details": None,
                "active": False,
            },
            {
                "source": "user",
                "value": "bar",
                "details": None,
                "active": True,
            },
        ]

    def test_serializes_lineage_details_for_transfer_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        lineage_type = Struct(
            kind="type",
            name="vector",
            type="vector",
            _root_kind="vector",
            attributes={
                "element_type": Struct(
                    kind="type",
                    name="mlody-lineage-event",
                    type="mlody-lineage-event",
                    type_name="mlody-lineage-event",
                    _root_kind="record",
                    attributes={},
                )
            },
        )
        lineage_events = [
            Struct(
                kind="lineage_event",
                source="downloaded from",
                new_value=Struct(kind="location", data="https://example.com/employees.csv"),
                details={
                    "kind": "remote-download",
                    "staged_path": "/tmp/mlody-remote-abc.csv",
                },
            ),
            Struct(
                kind="lineage_event",
                source="copied from",
                new_value=Struct(kind="location", data=":raw-employees-remote"),
                details={
                    "kind": "local-copy",
                    "destination_path": "/home/mav/.cache/mlody/employees.csv",
                },
            ),
        ]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="lineage",
                    type=lineage_type,
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._display_payload",
            lambda _value: lineage_events,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//pipeline:raw-employees.lineage",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["data"] == [
            {
                "source": "downloaded from",
                "value": "content of /tmp/mlody-remote-abc.csv",
                "details": {
                    "kind": "remote-download",
                    "staged_path": "/tmp/mlody-remote-abc.csv",
                },
                "active": False,
            },
            {
                "source": "copied from",
                "value": "content of /home/mav/.cache/mlody/employees.csv",
                "details": {
                    "kind": "local-copy",
                    "destination_path": "/home/mav/.cache/mlody/employees.csv",
                },
                "active": True,
            },
        ]

    def test_serializes_dag_values_as_stage_dag_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        graph = networkx.MultiDiGraph()
        value_type = SimpleNamespace(
            name="remote-file",
            type="remote-file",
            _root_kind="remote-file",
            attributes={},
        )
        dataset_type = SimpleNamespace(
            name="dataset",
            type="dataset",
            _root_kind="dataset",
            attributes={},
        )
        database_type = SimpleNamespace(
            name="database",
            type="database",
            _root_kind="database",
            attributes={},
        )

        graph.add_node(
            "value/test:raw-employees-local",
            value=ValueNode(
                node_id="value/test:raw-employees-local",
                name="raw-employees-local",
                value=SimpleNamespace(name="raw-employees-local", type=value_type),
            ),
        )
        graph.add_node(
            "task/test:cleanup",
            task=TaskNode(
                node_id="task/test:cleanup",
                name="cleanup",
                task=SimpleNamespace(
                    name="cleanup",
                    action=SimpleNamespace(name="cleanup-action"),
                    inputs={
                        "raw-employees-table": SimpleNamespace(
                            name="raw-employees-table",
                            type=dataset_type,
                        ),
                    },
                    outputs={
                        "employees-table": SimpleNamespace(
                            name="employees-table",
                            type=dataset_type,
                        ),
                    },
                    config={
                        "database": SimpleNamespace(
                            name="database",
                            type=database_type,
                        ),
                    },
                ),
            ),
        )
        graph.add_edge(
            "value/test:raw-employees-local",
            "task/test:cleanup",
            edge=Edge(
                src_port="raw-employees-local",
                dst_path="raw-employees-table",
            ),
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="employees-table",
                    label="//pipeline:cleanup.outputs.employees-table.dag",
                    type=MlodyDagType(),
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._display_payload",
            lambda _value: graph,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//pipeline:cleanup.outputs.employees-table.dag",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"] == {
            "type": "dag",
            "title": "DAG — ancestors of '//pipeline:cleanup.outputs.employees-table'",
            "nodeCount": 2,
            "edgeCount": 1,
        }

        data = response["data"]
        assert data["edges"] == [
            {
                "id": "edge-0",
                "sourceNodeId": "value/test:raw-employees-local",
                "sourcePortId": "out:raw-employees-local",
                "targetNodeId": "task/test:cleanup",
                "targetPortId": "in:raw-employees-table",
                "label": "raw-employees-local → raw-employees-table",
            }
        ]

        task_node = next(
            node for node in data["nodes"] if node["id"] == "task/test:cleanup"
        )
        assert task_node["kind"] == "task"
        assert task_node["title"] == "cleanup"
        assert task_node["subtitle"] == "cleanup-action"
        assert task_node["ports"] == [
            {
                "id": "in:raw-employees-table",
                "label": "raw-employees-table",
                "side": "input",
                "kind": "input",
                "typeLabel": "dataset",
            },
            {
                "id": "in:database",
                "label": "database",
                "side": "input",
                "kind": "config",
                "typeLabel": "database",
            },
            {
                "id": "out:employees-table",
                "label": "employees-table",
                "side": "output",
                "kind": "output",
                "typeLabel": "dataset",
            },
        ]

    def test_serializes_action_graph_values_as_stage_action_graph_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        graph = networkx.DiGraph()
        graph.add_node(
            "struct:task/test:cleanup",
            action=MlodyActionGraphNode(
                node_id="struct:task/test:cleanup",
                executor="mlody",
                operation="structural-task",
                title="Task Context",
                detail="cleanup",
                description="Loads the task node selected from the pruned task/value graph.",
                executor_detail="Runs in-process Python in the current mlody CLI/server runtime.",
                structural_node_id="task/test:cleanup",
            ),
        )
        graph.add_node(
            "prepare://pipeline:cleanup.outputs.employees-table",
            action=MlodyActionGraphNode(
                node_id="prepare://pipeline:cleanup.outputs.employees-table",
                executor="mlody",
                operation="prepare-show-value",
                title="Prepare Display",
                detail="//pipeline:cleanup.outputs.employees-table",
                description="Consumes the already-resolved requested value and runs show-time preparation: force virtual values, derive the display payload, and build a tabular preview when applicable.",
                executor_detail="Runs in-process Python in the current mlody CLI/server runtime.",
            ),
        )
        graph.add_edge(
            "struct:task/test:cleanup",
            "prepare://pipeline:cleanup.outputs.employees-table",
        )

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodyValueValue(
                struct=Struct(
                    kind="value",
                    name="employees-table",
                    label="//pipeline:cleanup.outputs.employees-table.agraph",
                    type=MlodyActionGraphType(),
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._display_payload",
            lambda _value: graph,
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//pipeline:cleanup.outputs.employees-table.agraph",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"] == {
            "type": "action-graph",
            "title": "Action Graph — plan for '//pipeline:cleanup.outputs.employees-table'",
            "nodeCount": 2,
            "edgeCount": 1,
        }

        data = response["data"]
        assert data["edges"] == [
            {
                "id": "edge-0",
                "sourceNodeId": "struct:task/test:cleanup",
                "targetNodeId": "prepare://pipeline:cleanup.outputs.employees-table",
            },
        ]

        task_node = next(
            node
            for node in data["nodes"]
            if node["id"] == "struct:task/test:cleanup"
        )
        assert task_node == {
            "id": "struct:task/test:cleanup",
            "kind": "task",
            "title": "Task Context",
            "subtitle": "cleanup",
            "description": "Loads the task node selected from the pruned task/value graph.",
            "executor": "mlody",
            "executorDetail": "Runs in-process Python in the current mlody CLI/server runtime.",
            "operation": "structural-task",
            "structuralNodeId": "task/test:cleanup",
            "position": task_node["position"],
        }

    def test_serializes_source_range_values_as_stage_source_code_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_path = tmp_path / "pipeline.mlody"
        source_path.write_text(
            "before = 1\n"
            "builtins.register(\n"
            "    \"value\",\n"
            "    struct(name=\"raw-employees-remote\"),\n"
            ")\n"
            "after = 2\n"
        )

        class _FakeWorkspace:
            evaluator = SimpleNamespace(_method_registry={})

            @staticmethod
            def expand_wildcard_label(label: str) -> list[str]:
                return [label]

        monkeypatch.setattr(
            "mlody.cli.server.resolve_workspace",
            lambda *args, **kwargs: (_FakeWorkspace(), "sha123"),
        )
        monkeypatch.setattr(
            "mlody.cli.server.resolve_label_to_value",
            lambda _label, _workspace: MlodySourceRangeValue(
                filepath="pipeline.mlody",
                abs_path=source_path,
                start_line=2,
                end_line=5,
            ),
        )

        request = parse_verbatim_command_request(
            {
                "command": "show",
                "input": "//pipeline:raw-employees-remote._source_range",
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert response["kind"] == "result"
        assert response["view"] == {
            "type": "source-code",
            "title": "//pipeline:raw-employees-remote._source_range",
        }
        assert response["data"] == {
            "path": "pipeline.mlody",
            "language": "python",
            "startLine": 2,
            "endLine": 5,
            "code": (
                "builtins.register(\n"
                "    \"value\",\n"
                "    struct(name=\"raw-employees-remote\"),\n"
                ")"
            ),
        }


class TestStageJsonData:
    def test_encodes_image_payloads_for_stage(self) -> None:
        payload = _stage_json_data(
            {
                "path": "027827.png",
                "bytes": b"\x89PNG\r\n\x1a\nfake",
            }
        )

        assert payload["kind"] == "encoded-image"
        assert payload["mimeType"] == "image/png"
        assert payload["path"] == "027827.png"
        assert payload["base64"] == "iVBORw0KGgpmYWtl"


class TestCommandHistoryParsing:
    def test_splits_remote_show_input_into_breadcrumb_and_prompt(self) -> None:
        breadcrumb, prompt = _history_prompt_and_breadcrumb(
            ServerCommandRequest(
                request_id="req-1",
                command="show",
                arguments=("@pixelle//datasets:celebA-dataset.train",),
                options={},
                input_text="@pixelle//datasets:celebA-dataset.train",
            )
        )

        assert breadcrumb == ["@pixelle", "//", "datasets:", "celebA-dataset"]
        assert prompt == ".train"

    def test_splits_absolute_show_input_into_breadcrumb_and_prompt(self) -> None:
        breadcrumb, prompt = _history_prompt_and_breadcrumb(
            ServerCommandRequest(
                request_id="req-2",
                command="show",
                arguments=("//projects/omega.summary",),
                options={},
                input_text="//projects/omega.summary",
            )
        )

        assert breadcrumb == ["//", "projects", "omega"]
        assert prompt == ".summary"

    def test_splits_wildcard_query_show_input_into_breadcrumb_and_prompt(self) -> None:
        breadcrumb, prompt = _history_prompt_and_breadcrumb(
            ServerCommandRequest(
                request_id="req-3",
                command="show",
                arguments=('//.../[@mlody _.kind == "user"]',),
                options={},
                input_text='//.../[@mlody _.kind == "user"]',
            )
        )

        assert breadcrumb == ["//", "..."]
        assert prompt == '[@mlody _.kind == "user"]'


class TestHttpApi:
    def test_root_serves_stage_index_html(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stage_root = tmp_path / "stage-static"
        avatar_root = tmp_path / "avatars"
        stage_root.mkdir()
        avatar_root.mkdir()
        (stage_root / "index.html").write_text("<!doctype html><title>Stage</title>")
        monkeypatch.setattr(
            "mlody.cli.server._stage_static_roots",
            lambda: (stage_root, avatar_root),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type")

            assert "<title>Stage</title>" in body
            assert content_type == "text/html; charset=utf-8"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)


class TestServerStartupErrors:
    def test_run_server_reports_clear_error_when_http_port_is_in_use(
        self,
        tmp_path: Path,
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            http_port = occupied.getsockname()[1]
            config = ServerConfig(
                monorepo_root=tmp_path,
                workspace_root=tmp_path,
                roots=None,
                verbose=False,
                full_workspace=False,
                http_host="127.0.0.1",
                http_port=http_port,
                lsp_host="127.0.0.1",
                lsp_port=0,
            )

            with pytest.raises(click.ClickException) as exc_info:
                from mlody.cli.server import run_server

                run_server(config)

        assert (
            exc_info.value.message
            == "Could not start HTTP API listener on 127.0.0.1:"
            f"{http_port}: address already in use. "
            "Stop the existing process or choose a different --server-port."
        )

    def test_run_server_reports_clear_error_when_lsp_port_is_in_use(
        self,
        tmp_path: Path,
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            lsp_port = occupied.getsockname()[1]
            config = ServerConfig(
                monorepo_root=tmp_path,
                workspace_root=tmp_path,
                roots=None,
                verbose=False,
                full_workspace=False,
                http_host="127.0.0.1",
                http_port=0,
                lsp_host="127.0.0.1",
                lsp_port=lsp_port,
            )

            with pytest.raises(click.ClickException) as exc_info:
                from mlody.cli.server import run_server

                run_server(config)

        assert (
            exc_info.value.message
            == "Could not start LSP listener on 127.0.0.1:"
            f"{lsp_port}: address already in use. "
            "Stop the existing process or choose a different --lsp-port."
        )

    def test_non_api_route_falls_back_to_stage_index_html(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stage_root = tmp_path / "stage-static"
        avatar_root = tmp_path / "avatars"
        stage_root.mkdir()
        avatar_root.mkdir()
        (stage_root / "index.html").write_text("<!doctype html><title>Stage SPA</title>")
        monkeypatch.setattr(
            "mlody.cli.server._stage_static_roots",
            lambda: (stage_root, avatar_root),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/settings",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")

            assert "<title>Stage SPA</title>" in body
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_bundle_js_served_via_runfiles_manifest_lookup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stage_root = tmp_path / "stage-static"
        avatar_root = tmp_path / "avatars"
        bundle_file = tmp_path / "bundle.js"
        stage_root.mkdir()
        avatar_root.mkdir()
        bundle_file.write_text("console.log('stage bundle');")
        monkeypatch.setattr(
            "mlody.cli.server._stage_static_roots",
            lambda: (stage_root, avatar_root),
        )
        monkeypatch.setattr(
            "mlody.cli.server._runfiles_path",
            lambda logical_path: bundle_file
            if logical_path == "_main/mlody/stage/bundle.js"
            else None,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/bundle.js",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type")

            assert "stage bundle" in body
            assert content_type == "text/javascript; charset=utf-8"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_avatar_asset_served_via_runfiles_manifest_lookup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stage_root = tmp_path / "stage-static"
        avatar_root = tmp_path / "avatars"
        avatar_file = tmp_path / "avatars-2-2.png"
        stage_root.mkdir()
        avatar_root.mkdir()
        avatar_file.write_bytes(b"fake-avatar-bytes")
        monkeypatch.setattr(
            "mlody.cli.server._stage_static_roots",
            lambda: (stage_root, avatar_root),
        )
        monkeypatch.setattr(
            "mlody.cli.server._runfiles_path",
            lambda logical_path: avatar_file
            if logical_path
            == "_main/mlody/assets/images/avatars/avatars-2-2.png"
            else None,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/assets/images/avatars/avatars-2-2.png",
                timeout=5,
            ) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type")

            assert body == b"fake-avatar-bytes"
            assert content_type == "image/png"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_healthz_reports_http_and_lsp_endpoints(self, tmp_path: Path) -> None:
        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/healthz",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert payload["status"] == "ok"
            assert payload["instanceId"] == http_server.server_config.instance_id
            assert payload["http"]["port"] == http_server.server_port
            assert payload["lsp"]["port"] == 8766
            assert payload["workspace"]["workspaceRoot"] == ""
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_server_status_reports_runtime_details(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        launch_cwd.mkdir()
        config = ServerConfig(
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            roots=None,
            verbose=True,
            full_workspace=False,
            http_host="127.0.0.1",
            http_port=0,
            lsp_host="127.0.0.1",
            lsp_port=8766,
            started_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
            started_monotonic=100.0,
            restart_cwd=launch_cwd,
            restart_argv=("mlody", "--server", "--server-port", "8765"),
            instance_id="server-instance",
        )
        monkeypatch.setattr("mlody.cli.server.time.monotonic", lambda: 160.25)
        monkeypatch.setattr("mlody.cli.server.os.getcwd", lambda: str(tmp_path))
        monkeypatch.setattr("mlody.cli.server.threading.active_count", lambda: 7)
        http_server = create_http_server(config)
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/api/server/status",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert payload["status"] == "ok"
            assert payload["instanceId"] == "server-instance"
            assert payload["pid"] == os.getpid()
            assert payload["startedAt"] == "2026-05-18T12:00:00Z"
            assert payload["uptimeSeconds"] == pytest.approx(60.25)
            assert payload["currentCwd"] == str(tmp_path)
            assert payload["launchCwd"] == str(launch_cwd)
            assert payload["launchArgv"] == [
                "mlody",
                "--server",
                "--server-port",
                "8765",
            ]
            assert payload["pythonExecutable"]
            assert payload["pythonVersion"]
            assert payload["platform"]
            assert payload["threadCount"] == 7
            assert payload["http"]["port"] == http_server.server_port
            assert payload["lsp"]["port"] == 8766
            assert payload["workspace"] == {
                "workspaceRoot": "",
                "roots": None,
                "fullWorkspace": False,
                "monorepoRoot": str(tmp_path),
            }
            assert payload["logging"] == {
                "verbose": True,
                "retainedStageRequestCount": 0,
                "retainedStageRequestCapacity": 200,
            }
            assert payload["restartPending"] is False
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_restart_endpoint_returns_accepted_and_schedules_restart(
        self,
        tmp_path: Path,
    ) -> None:
        http_server = create_http_server(_server_config(tmp_path))
        restart_calls: list[bool] = []

        def _fake_request_restart() -> str:
            restart_calls.append(True)
            return http_server.server_config.instance_id

        http_server.request_restart = _fake_request_restart  # type: ignore[method-assign]
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/server/restart",
                data=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert response.status == HTTPStatus.ACCEPTED
            assert payload == {
                "status": "restarting",
                "previousInstanceId": http_server.server_config.instance_id,
            }
            assert restart_calls == [True]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_restart_watcher_uses_original_cwd_and_argv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        restart_cwd = tmp_path / "launch"
        restart_cwd.mkdir()
        config = ServerConfig(
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            roots=None,
            verbose=False,
            full_workspace=False,
            http_host="127.0.0.1",
            http_port=0,
            lsp_host="127.0.0.1",
            lsp_port=8766,
            restart_cwd=restart_cwd,
            restart_argv=("mlody", "--server", "--server-port", "8765"),
            instance_id="server-instance",
        )
        popen_calls: list[tuple[list[str], dict[str, object]]] = []

        def _fake_popen(argv: list[str], **kwargs: object) -> SimpleNamespace:
            popen_calls.append((argv, dict(kwargs)))
            return SimpleNamespace()

        monkeypatch.setattr("mlody.cli.server.subprocess.Popen", _fake_popen)

        _spawn_restart_watcher(config, parent_pid=4321)

        assert len(popen_calls) == 1
        launcher_argv, launcher_kwargs = popen_calls[0]
        assert launcher_argv[1] == "-c"
        assert launcher_argv[3] == "4321"
        assert launcher_argv[4] == str(restart_cwd)
        assert json.loads(launcher_argv[5]) == list(config.restart_argv)
        assert launcher_kwargs["cwd"] == str(restart_cwd)
        assert launcher_kwargs["close_fds"] is True
        assert launcher_kwargs["start_new_session"] is True

    def test_stream_endpoint_emits_ndjson(self, tmp_path: Path) -> None:
        def _fake_event_source(config: ServerConfig, request) -> list[dict[str, object]]:
            return [
                {
                    "requestId": request.request_id,
                    "event": "started",
                    "command": request.command,
                    "arguments": list(request.arguments),
                },
                {
                    "requestId": request.request_id,
                    "event": "result",
                    "value": {"kind": "folder", "payload": {"path": "artifacts"}},
                },
                {
                    "requestId": request.request_id,
                    "event": "completed",
                    "status": "done",
                    "resultCount": 1,
                    "errorCount": 0,
                },
            ]

        http_server = create_http_server(
            _server_config(tmp_path),
            event_source=_fake_event_source,
        )
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stream",
                data=json.dumps(
                    {"requestId": "req-stream", "command": "show", "targets": ["x"]}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload_lines = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                ]
                content_type = response.headers.get("Content-Type")
                cors_origin = response.headers.get("Access-Control-Allow-Origin")

            assert content_type == "application/x-ndjson; charset=utf-8"
            assert cors_origin == "*"
            assert payload_lines[0]["event"] == "started"
            assert payload_lines[-1]["event"] == "completed"
            assert payload_lines[-1]["status"] == "done"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_verbatim_execute_endpoint_returns_full_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _fake_execute(config: ServerConfig, request):
            assert request.arguments == (
                "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
            )
            return {
                "requestId": request.request_id,
                "command": request.command,
                "status": "done",
                "exitCode": 0,
                "output": "Value for user 'mav'\nrow 1\n",
            }

        monkeypatch.setattr(
            "mlody.cli.server.execute_verbatim_command_response",
            _fake_execute,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/verbatim",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert payload["status"] == "done"
            assert payload["output"] == "Value for user 'mav'\nrow 1\n"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_execute_endpoint_returns_stage_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "mlody.cli.server._collect_stage_command_response",
            lambda _config, request, *, event_source=None: (
                {
                    "kind": "result",
                    "requestId": request.request_id,
                    "view": {
                        "type": "table",
                        "title": "Employees",
                        "columns": [
                            {"key": "name", "label": "Name"},
                            {"key": "salary", "label": "Salary", "format": "currency"},
                        ],
                    },
                    "data": [
                        {"name": "Ada", "salary": 120000},
                        {"name": "Grace", "salary": 135000},
                    ],
                },
                [
                    {
                        "kind": "result",
                        "requestId": request.request_id,
                        "command": "show",
                        "label": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                    }
                ],
            ),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stage",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert payload["kind"] == "result"
            assert payload["requestId"]
            assert payload["view"]["type"] == "table"
            assert payload["data"][0]["name"] == "Ada"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_execute_logs_endpoint_returns_structured_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "mlody.cli.server._collect_stage_command_response",
            lambda _config, request, *, event_source=None: (
                {
                    "kind": "result",
                    "requestId": request.request_id,
                    "view": {"type": "json", "title": "Stub"},
                    "data": {"ok": True},
                },
                [
                    {
                        "kind": "chunk",
                        "requestId": request.request_id,
                        "channel": "stdout",
                        "text": "Resolving label...",
                    },
                    {
                        "kind": "result",
                        "requestId": request.request_id,
                        "command": "show",
                        "label": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                        "stageResult": {"kind": "result"},
                        "value": {"kind": "table"},
                    },
                ],
            ),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            execute_request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stage",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(execute_request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            logs_url = (
                f"http://127.0.0.1:{http_server.server_port}"
                f"/api/execute/stage/logs/{payload['requestId']}"
            )
            with urlopen(logs_url, timeout=5) as response:
                logs_payload = json.loads(response.read().decode("utf-8"))

            assert logs_payload["requestId"] == payload["requestId"]
            assert logs_payload["events"] == [
                {
                    "kind": "chunk",
                    "requestId": payload["requestId"],
                    "channel": "stdout",
                    "text": "Resolving label...",
                },
                {
                    "kind": "result",
                    "requestId": payload["requestId"],
                    "command": "show",
                    "label": "@pixelle//datasets:celebA-dataset.train[@sql limit 2]",
                },
            ]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_execute_logs_endpoint_captures_python_logger_output(
        self,
        tmp_path: Path,
    ) -> None:
        logger = logging.getLogger("mlody.core.assets.http_asset")
        previous_level = logger.level
        logger.setLevel(logging.INFO)

        def _event_source(_config: ServerConfig, request: ServerCommandRequest):
            logger.info(
                "Reusing cached remote URI %s from %s",
                "https://example.com/data.csv",
                "/tmp/payload.csv",
            )
            yield {
                "requestId": request.request_id,
                "event": "result",
                "command": "show",
                "stageResult": {
                    "kind": "result",
                    "view": {"type": "json", "title": "Stub"},
                    "data": {"ok": True},
                },
            }
            yield {
                "requestId": request.request_id,
                "event": "completed",
                "status": "done",
                "resultCount": 1,
                "errorCount": 0,
            }

        http_server = create_http_server(
            _server_config(tmp_path),
            event_source=_event_source,
        )
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            execute_request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stage",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "//pipeline:raw-employees-remote",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(execute_request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            logs_url = (
                f"http://127.0.0.1:{http_server.server_port}"
                f"/api/execute/stage/logs/{payload['requestId']}"
            )
            with urlopen(logs_url, timeout=5) as response:
                logs_payload = json.loads(response.read().decode("utf-8"))

            log_events = [
                event
                for event in logs_payload["events"]
                if event.get("event") == "log"
            ]
            assert len(log_events) == 1
            assert log_events[0]["level"] == "INFO"
            assert log_events[0]["logger"] == "mlody.core.assets.http_asset"
            assert (
                log_events[0]["message"]
                == "Reusing cached remote URI https://example.com/data.csv from /tmp/payload.csv"
            )
            assert log_events[0]["template"] == "Reusing cached remote URI %s from %s"
            assert log_events[0]["values"] == [
                "https://example.com/data.csv",
                "/tmp/payload.csv",
            ]
        finally:
            logger.setLevel(previous_level)
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_execute_logs_endpoint_captures_debug_logger_output(
        self,
        tmp_path: Path,
    ) -> None:
        root_logger = logging.getLogger()
        logger = logging.getLogger("mlody.core.assets.http_asset")
        previous_root_level = root_logger.level
        previous_logger_level = logger.level
        root_logger.setLevel(logging.DEBUG)
        logger.setLevel(logging.NOTSET)

        def _event_source(_config: ServerConfig, request: ServerCommandRequest):
            logger.debug(
                "Using cached remote metadata for %s",
                "https://example.com/data.csv",
            )
            yield {
                "requestId": request.request_id,
                "event": "result",
                "command": "show",
                "stageResult": {
                    "kind": "result",
                    "view": {"type": "json", "title": "Stub"},
                    "data": {"ok": True},
                },
            }
            yield {
                "requestId": request.request_id,
                "event": "completed",
                "status": "done",
                "resultCount": 1,
                "errorCount": 0,
            }

        http_server = create_http_server(
            _server_config(tmp_path),
            event_source=_event_source,
        )
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            execute_request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stage",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "//pipeline:raw-employees-remote",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(execute_request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            logs_url = (
                f"http://127.0.0.1:{http_server.server_port}"
                f"/api/execute/stage/logs/{payload['requestId']}"
            )
            with urlopen(logs_url, timeout=5) as response:
                logs_payload = json.loads(response.read().decode("utf-8"))

            log_events = [
                event
                for event in logs_payload["events"]
                if event.get("event") == "log"
            ]
            assert len(log_events) == 1
            assert log_events[0]["level"] == "DEBUG"
            assert log_events[0]["logger"] == "mlody.core.assets.http_asset"
            assert (
                log_events[0]["message"]
                == "Using cached remote metadata for https://example.com/data.csv"
            )
        finally:
            root_logger.setLevel(previous_root_level)
            logger.setLevel(previous_logger_level)
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_autocomplete_endpoint_returns_json_payload_and_headers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "mlody.cli.server.execute_stage_autocomplete_response",
            lambda _config, _request: {
                "completions": [{"label": "pixelle", "kind": "root"}],
                "additionalData": {},
            },
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/autocomplete/stage",
                data=json.dumps(
                    {
                        "workspaceRoot": None,
                        "breadcrumb": [],
                        "prompt": "@p",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                content_type = response.headers.get("Content-Type")
                cors_origin = response.headers.get("Access-Control-Allow-Origin")

            assert payload == {
                "completions": [{"label": "pixelle", "kind": "root"}],
                "additionalData": {},
            }
            assert content_type == "application/json; charset=utf-8"
            assert cors_origin == "*"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_autocomplete_endpoint_rejects_workspace_outside_monorepo(
        self,
        tmp_path: Path,
    ) -> None:
        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/autocomplete/stage",
                data=json.dumps(
                    {
                        "workspaceRoot": "/tmp/outside-monorepo",
                        "breadcrumb": [],
                        "prompt": "@p",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(HTTPError) as excinfo:
                urlopen(request, timeout=5)

            payload = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == HTTPStatus.BAD_REQUEST
            assert payload["error"] == "Workspace root must be relative to the monorepo root."
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_execute_endpoint_appends_command_history(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        history_path = tmp_path / ".cache" / "mlody" / "history.json"
        selected_workspace_root = tmp_path / "sandboxes" / "exp1"
        selected_workspace_root.mkdir(parents=True)
        fake_workspace = SimpleNamespace(
            info=SimpleNamespace(
                path=str(selected_workspace_root),
                branch="main",
                sha="abc123",
                roots=["pixelle"],
            ),
            root_infos={},
            evaluator=SimpleNamespace(_extra_ctx=SimpleNamespace()),
        )
        captured_workspace_roots: list[Path] = []
        monkeypatch.setattr(
            "mlody.cli.server._history_file_path",
            lambda: history_path,
        )
        monkeypatch.setattr(
            "mlody.cli.server._current_baseline_workspace",
            lambda config: captured_workspace_roots.append(config.workspace_root)
            or fake_workspace,
        )
        monkeypatch.setattr(
            "mlody.cli.server._collect_stage_command_response",
            lambda _config, request, *, event_source=None: (
                {
                    "kind": "result",
                    "requestId": request.request_id,
                    "view": {"type": "json", "title": "Stub"},
                    "data": {"ok": True},
                },
                [
                    {
                        "kind": "result",
                        "requestId": request.request_id,
                        "command": "show",
                        "label": "@pixelle//datasets:celebA-dataset.train",
                    }
                ],
            ),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{http_server.server_port}/api/execute/stage",
                data=json.dumps(
                    {
                        "command": "show",
                        "input": "@pixelle//datasets:celebA-dataset.train",
                        "options": {
                            "runAs": "agarcia",
                            "workspaceRoot": "sandboxes/exp1",
                        },
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5):
                pass

            payload = json.loads(history_path.read_text(encoding="utf-8"))

            assert len(payload) == 1
            assert payload[0]["command"] == "show"
            assert payload[0]["prompt"] == ".train"
            assert payload[0]["breadcrumb"] == [
                "@pixelle",
                "//",
                "datasets:",
                "celebA-dataset",
            ]
            assert payload[0]["currentUserName"] == "agarcia"
            assert payload[0]["workspace"]["info"]["sha"] == "abc123"
            assert payload[0]["workspace"]["workspaceRoot"] == "sandboxes/exp1"
            assert "monorepoRoot" not in payload[0]["workspace"]
            assert captured_workspace_roots == [selected_workspace_root.resolve()]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_users_endpoint_returns_registered_users(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_workspace = SimpleNamespace(
            evaluator=SimpleNamespace(
                registry=SimpleNamespace(
                    users=SimpleNamespace(
                        by_name={
                            "maya": SimpleNamespace(
                                name="maya",
                                description="Maya Patel",
                                groups=["operator", "admin"],
                            ),
                            "alex": SimpleNamespace(
                                name="alex",
                                description="Alex Rivera",
                                groups=["viewer"],
                            ),
                        }
                    )
                )
            )
        )
        monkeypatch.setattr(
            "mlody.cli.server._current_baseline_workspace",
            lambda _config: fake_workspace,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/api/users",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                cors_origin = response.headers.get("Access-Control-Allow-Origin")

            assert cors_origin == "*"
            assert [item["name"] for item in payload] == ["alex", "maya"]
            assert payload[0]["description"] == "Alex Rivera"
            assert payload[1]["groups"] == ["operator", "admin"]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_stage_query_list_endpoint_returns_supported_entity_rows(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        selected_workspace_root = tmp_path / "sandboxes" / "query"
        selected_workspace_root.mkdir(parents=True)
        captured_workspace_roots: list[Path] = []
        fake_workspace = SimpleNamespace(
            root_infos={
                "lexica": SimpleNamespace(
                    name="lexica",
                    path="//mlody/teams/lexica",
                    description="Text ML team",
                ),
                "workspace": SimpleNamespace(
                    name="workspace",
                    path="//sandboxes/query",
                    description="Injected workspace root",
                ),
            },
            registry_view=SimpleNamespace(
                iter_registry_items=lambda: (
                    (
                        ("user", "", "maya"),
                        SimpleNamespace(
                            description="Maya Patel",
                            avatar="assets/images/avatars/avatars-1-0.png",
                            groups=["framera", "framera-admin"],
                        ),
                    ),
                    (
                        ("task", "pipelines/train", "train_model"),
                        SimpleNamespace(description="Train the model"),
                    ),
                    (
                        ("type", "mlody/common", "record"),
                        SimpleNamespace(description="Record type"),
                    ),
                    (
                        ("location", "mlody/common", "s3"),
                        SimpleNamespace(description="Shared object storage"),
                    ),
                    (
                        ("value", "pipelines/train", "artifact"),
                        SimpleNamespace(description="Top-level artifact"),
                    ),
                )
            ),
        )

        def _fake_workspace_for_root(_config: ServerConfig, workspace_root: Path) -> SimpleNamespace:
            captured_workspace_roots.append(workspace_root)
            return fake_workspace

        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            _fake_workspace_for_root,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            entity_expectations = {
                "teams": ("Teams", "lexica", "Text ML team"),
                "users": ("Users", "maya", "Maya Patel"),
                "tasks": ("Tasks", "train_model", "Train the model"),
                "types": ("Types", "record", "Record type"),
                ("locations"): ("Locations", "s3", "Shared object storage"),
                "values": ("Top-level Values", "artifact", "Top-level artifact"),
            }

            for entity, (title, name, description) in entity_expectations.items():
                request = Request(
                    f"http://127.0.0.1:{http_server.server_port}/api/query/stage/list",
                    data=json.dumps(
                        {
                            "entity": entity,
                            "workspaceRoot": "sandboxes/query",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))

                assert payload["kind"] == "result"
                assert payload["view"]["type"] == "query-list"
                assert payload["view"]["title"] == title
                assert payload["view"]["entity"] == entity
                if entity == "users":
                    assert payload["data"] == [
                        {
                            "name": name,
                            "description": description,
                            "avatar": "assets/images/avatars/avatars-1-0.png",
                            "groups": ["framera", "framera-admin"],
                        }
                    ]
                else:
                    assert payload["data"] == [
                        {
                            "name": name,
                            "description": description,
                        }
                    ]

            assert captured_workspace_roots == [
                selected_workspace_root.resolve(),
                selected_workspace_root.resolve(),
                selected_workspace_root.resolve(),
                selected_workspace_root.resolve(),
                selected_workspace_root.resolve(),
                selected_workspace_root.resolve(),
            ]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_history_endpoint_returns_persisted_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        history_path = tmp_path / ".cache" / "mlody" / "history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(
                [
                    {
                        "id": "history-1",
                        "createdAt": "2026-05-13T12:00:00Z",
                        "command": "show",
                        "prompt": ".summary",
                        "breadcrumb": ["projects", "omega"],
                        "currentUserName": "mav",
                        "workspace": {"workspaceRoot": ""},
                    }
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "mlody.cli.server._history_file_path",
            lambda: history_path,
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/api/history",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert payload[0]["id"] == "history-1"
            assert payload[0]["breadcrumb"] == ["projects", "omega"]
            assert payload[0]["currentUserName"] == "mav"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

    def test_workspaces_endpoint_returns_available_workspace_summaries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_root = tmp_path / "sandboxes" / "exp1"
        workspace_root.mkdir(parents=True)
        available_roots = (tmp_path, workspace_root)

        def _fake_workspace(workspace_root: Path) -> SimpleNamespace:
            return SimpleNamespace(
                info=SimpleNamespace(
                    path=str(workspace_root),
                    branch="main",
                    sha="abc123",
                    roots=["workspace"],
                ),
                root_infos={},
                evaluator=SimpleNamespace(_extra_ctx=SimpleNamespace()),
            )

        monkeypatch.setattr(
            "mlody.cli.server._available_workspace_roots",
            lambda _config: available_roots,
        )
        monkeypatch.setattr(
            "mlody.cli.server._baseline_workspace_for_root",
            lambda _config, root: _fake_workspace(root),
        )

        http_server = create_http_server(_server_config(tmp_path))
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/api/workspaces",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert [item["workspaceRoot"] for item in payload] == [
                "",
                "sandboxes/exp1",
            ]
            assert payload[1]["info"]["path"] == str(workspace_root)
            assert "monorepoRoot" not in payload[0]
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)


class _BrokenPipeWriter:
    def write(self, _data: bytes) -> int:
        raise BrokenPipeError("client disconnected")

    def flush(self) -> None:
        raise BrokenPipeError("client disconnected")


class TestResponseWriters:
    def test_write_json_response_ignores_client_disconnect(self) -> None:
        handler = MlodyApiRequestHandler.__new__(MlodyApiRequestHandler)
        handler.send_response = lambda *args, **kwargs: None
        handler._send_common_headers = lambda: None
        handler.send_header = lambda *args, **kwargs: None
        handler.end_headers = lambda: None
        handler.wfile = _BrokenPipeWriter()

        handler._write_json_response(HTTPStatus.OK, {"status": "ok"})

    def test_write_ndjson_response_ignores_client_disconnect(self) -> None:
        handler = MlodyApiRequestHandler.__new__(MlodyApiRequestHandler)
        handler.send_response = lambda *args, **kwargs: None
        handler._send_common_headers = lambda: None
        handler.send_header = lambda *args, **kwargs: None
        handler.end_headers = lambda: None
        handler.wfile = _BrokenPipeWriter()

        handler._write_ndjson_response(iter([{"event": "chunk"}]))

    def test_workspace_endpoint_returns_loaded_workspace_info(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_workspace = SimpleNamespace(
            info=SimpleNamespace(
                path=str(tmp_path),
                branch="main",
                sha="abc123",
                roots=["lexica", "workspace"],
            ),
            root_infos={
                "workspace": SimpleNamespace(
                    name="workspace",
                    path="//sandboxes/exp1",
                    description="injected",
                ),
                "lexica": SimpleNamespace(
                    name="lexica",
                    path="//mlody/teams/lexica",
                    description="text ML team",
                ),
            },
            evaluator=SimpleNamespace(
                _extra_ctx=SimpleNamespace(
                    workspace=SimpleNamespace(
                        directory=str(tmp_path / "sandboxes" / "exp1"),
                        user="maya",
                    ),
                    run=SimpleNamespace(user="mav"),
                )
            ),
        )
        monkeypatch.setattr(
            "mlody.cli.server._current_baseline_workspace",
            lambda _config: fake_workspace,
        )

        config = _server_config(tmp_path)
        config = ServerConfig(
            monorepo_root=config.monorepo_root,
            workspace_root=tmp_path / "sandboxes" / "exp1",
            roots=tmp_path / "mlody" / "custom-roots.mlody",
            verbose=config.verbose,
            full_workspace=True,
            http_host=config.http_host,
            http_port=config.http_port,
            lsp_host=config.lsp_host,
            lsp_port=config.lsp_port,
        )
        http_server = create_http_server(config)
        server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with urlopen(
                f"http://127.0.0.1:{http_server.server_port}/api/workspace",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

            assert "monorepoRoot" not in payload
            assert payload["workspaceRoot"] == "sandboxes/exp1"
            assert payload["rootsFile"] == str(tmp_path / "mlody" / "custom-roots.mlody")
            assert payload["fullWorkspace"] is True
            assert payload["info"]["branch"] == "main"
            assert [item["name"] for item in payload["rootInfos"]] == [
                "lexica",
                "workspace",
            ]
            assert payload["context"]["workspace"]["user"] == "maya"
            assert payload["context"]["run"]["user"] == "mav"
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)
