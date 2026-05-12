"""Tests for persistent mlody CLI server mode."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from mlody.cli.server import (
    ServerConfig,
    collect_command_response,
    create_http_server,
    parse_command_request,
)
from mlody.resolver import MlodyFolderValue


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
