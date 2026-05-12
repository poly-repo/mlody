"""Tests for persistent mlody CLI server mode."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from mlody.cli.server import (
    MlodyApiRequestHandler,
    ServerConfig,
    _stage_json_data,
    collect_command_response,
    create_http_server,
    execute_stage_command_response,
    execute_verbatim_command_response,
    parse_command_request,
    parse_verbatim_command_request,
)
from mlody.resolver import MlodyFolderValue
from mlody.resolver.label_value import _RawAttrValue


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
            }
        )
        response = execute_verbatim_command_response(_server_config(tmp_path), request)

        assert captured["args"] == [
            "show",
            "--as",
            "mav",
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
            }
        )
        response = execute_stage_command_response(_server_config(tmp_path), request)

        assert captured["target"] == "@pixelle//datasets:celebA-dataset.train[@sql limit 2]"
        assert captured["workspace_root"] == tmp_path
        assert captured["user"] == "mav"
        assert response["kind"] == "result"
        assert response["view"]["type"] == "json"
        assert response["data"]["kind"] == "folder"
        assert response["data"]["payload"]["path"] == "artifacts"

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


class TestHttpApi:
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
            assert payload["http"]["port"] == http_server.server_port
            assert payload["lsp"]["port"] == 8766
            assert payload["workspace"]["monorepoRoot"] == str(tmp_path)
        finally:
            http_server.shutdown()
            http_server.server_close()
            server_thread.join(timeout=5)

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
            "mlody.cli.server.execute_stage_command_response",
            lambda _config, _request: {
                "kind": "result",
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
            assert payload["view"]["type"] == "table"
            assert payload["data"][0]["name"] == "Ada"
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

            assert payload["monorepoRoot"] == str(tmp_path)
            assert payload["workspaceRoot"] == str(tmp_path / "sandboxes" / "exp1")
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
