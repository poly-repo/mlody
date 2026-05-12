"""Persistent server mode for mlody CLI.

This module exposes two transports side-by-side:

* HTTP/JSON for browser and tool integrations.
* TCP LSP, reusing the existing pygls-based server from ``mlody.lsp``.

The HTTP API supports both one-shot JSON responses and streaming NDJSON so the
frontend can consume incremental state updates for long-running commands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import shlex
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import click

from common.python.starlarkish.evaluator.evaluator import _runtime_json_data
from mlody.cli.show import _describe_mlody_value, _parse_inner, _selected_show_user
from mlody.core.label import parse_label
from mlody.core.label.label import Label
from mlody.core.workspace import WorkspaceLoadError, force
from mlody.resolver import (
    MlodyActionValue,
    MlodyFolderValue,
    MlodySourceValue,
    MlodyTaskValue,
    MlodyUnresolvedValue,
    MlodyValue,
    MlodyValueValue,
    MlodyVectorValue,
    MlodyWorkspaceValue,
    resolve_label_to_value,
    resolve_workspace,
)
from mlody.resolver.errors import WorkspaceResolutionError

_logger = logging.getLogger(__name__)

_JSON_MIME = "application/json; charset=utf-8"
_NDJSON_MIME = "application/x-ndjson; charset=utf-8"


@dataclass(frozen=True)
class ServerConfig:
    """Static configuration for persistent server mode."""

    monorepo_root: Path
    workspace_root: Path
    roots: Path | None
    verbose: bool
    full_workspace: bool
    http_host: str
    http_port: int
    lsp_host: str
    lsp_port: int


@dataclass(frozen=True)
class ServerCommandRequest:
    """Normalized command request accepted by the HTTP API."""

    request_id: str
    command: str
    arguments: tuple[str, ...]
    options: dict[str, object]
    input_text: str = ""


class ServerRequestError(ValueError):
    """Raised when an incoming HTTP request cannot be parsed or validated."""


CommandEvent = dict[str, object]
CommandEventSource = Callable[[ServerConfig, ServerCommandRequest], Iterator[CommandEvent]]


def _compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _event(
    request: ServerCommandRequest,
    event_name: str,
    **payload: object,
) -> CommandEvent:
    event_payload: CommandEvent = {
        "requestId": request.request_id,
        "event": event_name,
    }
    event_payload.update(payload)
    return event_payload


def _completion_status(*, result_count: int, error_count: int) -> str:
    if error_count == 0:
        return "done"
    if result_count > 0:
        return "partial_error"
    return "error"


def _read_string_option(
    options: Mapping[str, object],
    *names: str,
    default: str,
) -> str:
    for name in names:
        value = options.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ServerRequestError(f"Option '{name}' must be a string.")
        return value
    return default


def _read_string_list_option(
    options: Mapping[str, object],
    *names: str,
) -> list[str]:
    for name in names:
        value = options.get(name)
        if value is None:
            continue
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ServerRequestError(f"Option '{name}' must be a list of strings.")
        values = list(value)
        if not all(isinstance(item, str) for item in values):
            raise ServerRequestError(f"Option '{name}' must be a list of strings.")
        return cast(list[str], values)
    return []


def parse_command_request(payload: object) -> ServerCommandRequest:
    """Validate and normalize an incoming JSON command payload."""

    if not isinstance(payload, Mapping):
        raise ServerRequestError("Request body must be a JSON object.")

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ServerRequestError("Field 'command' must be a non-empty string.")
    normalized_command = command.strip()

    request_id = payload.get("requestId")
    if request_id is None:
        normalized_request_id = uuid.uuid4().hex
    elif isinstance(request_id, str) and request_id.strip():
        normalized_request_id = request_id.strip()
    else:
        raise ServerRequestError("Field 'requestId' must be a non-empty string.")

    input_text = payload.get("input", "")
    if not isinstance(input_text, str):
        raise ServerRequestError("Field 'input' must be a string when provided.")

    raw_options = payload.get("options", {})
    if not isinstance(raw_options, Mapping):
        raise ServerRequestError("Field 'options' must be a JSON object when provided.")

    options = dict(raw_options)
    for alias in ("runAs", "run_as", "config", "with"):
        if alias in payload and alias not in options:
            options[alias] = payload[alias]

    raw_arguments = payload.get("targets")
    if raw_arguments is None:
        raw_arguments = payload.get("arguments")
    if raw_arguments is None:
        arguments = tuple(shlex.split(input_text))
    else:
        if not isinstance(raw_arguments, Sequence) or isinstance(
            raw_arguments, (str, bytes)
        ):
            raise ServerRequestError(
                "Fields 'arguments' and 'targets' must be arrays of strings."
            )
        values = list(raw_arguments)
        if not all(isinstance(item, str) for item in values):
            raise ServerRequestError(
                "Fields 'arguments' and 'targets' must be arrays of strings."
            )
        arguments = tuple(cast(list[str], values))

    if normalized_command == "show" and not arguments:
        raise ServerRequestError("Show requests require at least one target.")

    return ServerCommandRequest(
        request_id=normalized_request_id,
        command=normalized_command,
        arguments=arguments,
        options=options,
        input_text=input_text,
    )


def _concrete_show_label(committoid: str | None, label_text: str) -> Label:
    if label_text == "":
        return Label(
            workspace=committoid,
            workspace_query=None,
            entity=None,
            entity_query=None,
            attribute_path=None,
            attribute_query=None,
        )
    return parse_label(label_text)


def _serialize_mlody_value(value: MlodyValue) -> dict[str, object]:
    display_text = _describe_mlody_value(value)

    if isinstance(value, MlodyWorkspaceValue):
        payload: object = {
            "name": value.name,
            "root": value.root,
        }
        return {"kind": "workspace", "payload": payload, "displayText": display_text}

    if isinstance(value, MlodyFolderValue):
        payload = {
            "path": value.path,
            "children": value.children,
        }
        return {"kind": "folder", "payload": payload, "displayText": display_text}

    if isinstance(value, MlodySourceValue):
        payload = {
            "path": value.path,
            "absPath": str(value.abs_path) if value.abs_path is not None else None,
        }
        return {"kind": "source", "payload": payload, "displayText": display_text}

    if isinstance(value, (MlodyTaskValue, MlodyActionValue, MlodyValueValue)):
        payload = _runtime_json_data(force(value.struct))
        kind = "value"
        if isinstance(value, MlodyTaskValue):
            kind = "task"
        elif isinstance(value, MlodyActionValue):
            kind = "action"
        return {"kind": kind, "payload": payload, "displayText": display_text}

    if isinstance(value, MlodyVectorValue):
        payload = [_serialize_mlody_value(element) for element in value.elements]
        return {"kind": "vector", "payload": payload, "displayText": display_text}

    if isinstance(value, MlodyUnresolvedValue):
        payload = {
            "label": repr(value.label),
            "reason": value.reason,
        }
        return {"kind": "unresolved", "payload": payload, "displayText": display_text}

    fallback_payload: object
    if hasattr(value, "__dict__"):
        fallback_payload = _runtime_json_data(vars(value))
    else:
        fallback_payload = repr(value)
    return {"kind": "unknown", "payload": fallback_payload, "displayText": display_text}


def _execute_show_command(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> Iterator[CommandEvent]:
    config_overrides = _read_string_list_option(request.options, "config", "with")
    run_as = _read_string_option(
        request.options, "runAs", "run_as", default="mav"
    )

    for target in request.arguments:
        yield _event(
            request,
            "chunk",
            kind="meta",
            text=f"Resolving {target}",
            target=target,
        )

        try:
            workspace, resolved_sha = resolve_workspace(
                target,
                monorepo_root=config.monorepo_root,
                workspace_root=config.workspace_root,
                config=config_overrides,
                user=run_as,
                roots_file=config.roots,
                full_workspace=config.full_workspace,
                verbose=config.verbose,
            )
            selected_user = _selected_show_user(workspace, run_as)
            committoid, inner_label = _parse_inner(target)

            for expanded_inner in workspace.expand_wildcard_label(inner_label):
                full_label = (
                    f"{committoid}|{expanded_inner}" if committoid else expanded_inner
                )
                concrete_label = _concrete_show_label(committoid, expanded_inner)
                mlody_value = resolve_label_to_value(concrete_label, workspace)
                if isinstance(mlody_value, MlodyUnresolvedValue):
                    yield _event(
                        request,
                        "error",
                        target=full_label,
                        message=mlody_value.reason,
                    )
                    continue

                yield _event(
                    request,
                    "result",
                    command="show",
                    sourceTarget=target,
                    target=full_label,
                    user=selected_user,
                    resolvedSha=resolved_sha,
                    value=_serialize_mlody_value(mlody_value),
                )
        except (
            WorkspaceLoadError,
            WorkspaceResolutionError,
            KeyError,
            AttributeError,
            ValueError,
        ) as exc:
            yield _event(request, "error", target=target, message=str(exc))


def iter_command_events(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> Iterator[CommandEvent]:
    """Stream events for a single command request."""

    result_count = 0
    error_count = 0

    yield _event(
        request,
        "started",
        command=request.command,
        arguments=list(request.arguments),
    )

    try:
        if request.command == "show":
            command_events = _execute_show_command(config, request)
        else:
            raise ServerRequestError(f"Unsupported command: {request.command}")

        for event in command_events:
            if event["event"] == "result":
                result_count += 1
            elif event["event"] == "error":
                error_count += 1
            yield event
    except ServerRequestError as exc:
        error_count += 1
        yield _event(request, "error", message=str(exc))
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Unhandled server error while executing %s", request.command)
        error_count += 1
        yield _event(request, "error", message=str(exc))

    yield _event(
        request,
        "completed",
        status=_completion_status(result_count=result_count, error_count=error_count),
        resultCount=result_count,
        errorCount=error_count,
    )


def collect_command_response(
    config: ServerConfig,
    request: ServerCommandRequest,
    *,
    event_source: CommandEventSource = iter_command_events,
) -> dict[str, object]:
    """Collect a command stream into a single JSON response payload."""

    events = list(event_source(config, request))
    results = [event for event in events if event.get("event") == "result"]
    errors = [event for event in events if event.get("event") == "error"]
    final_status = "error"
    for event in reversed(events):
        if event.get("event") == "completed":
            status = event.get("status")
            if isinstance(status, str):
                final_status = status
            break

    return {
        "requestId": request.request_id,
        "command": request.command,
        "status": final_status,
        "events": events,
        "results": results,
        "errors": errors,
    }


class MlodyApiServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying mlody server configuration."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        config: ServerConfig,
        event_source: CommandEventSource = iter_command_events,
    ) -> None:
        self.server_config = config
        self.event_source = event_source
        super().__init__(server_address, MlodyApiRequestHandler)


class MlodyApiRequestHandler(BaseHTTPRequestHandler):
    """Serve the HTTP JSON API for stage and other local clients."""

    server: MlodyApiServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        _logger.debug("HTTP %s - %s", self.address_string(), format % args)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/healthz":
            self._write_json_response(
                HTTPStatus.NOT_FOUND,
                {"error": f"Unknown endpoint: {path}"},
            )
            return

        self._write_json_response(
            HTTPStatus.OK,
            {
                "status": "ok",
                "http": {
                    "host": self.server.server_address[0],
                    "port": self.server.server_port,
                },
                "lsp": {
                    "host": self.server.server_config.lsp_host,
                    "port": self.server.server_config.lsp_port,
                    "transport": "tcp",
                },
                "workspace": {
                    "monorepoRoot": str(self.server.server_config.monorepo_root),
                    "workspaceRoot": str(self.server.server_config.workspace_root),
                    "roots": str(self.server.server_config.roots)
                    if self.server.server_config.roots is not None
                    else None,
                    "fullWorkspace": self.server.server_config.full_workspace,
                },
            },
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        try:
            payload = self._read_json_payload()
            request = parse_command_request(payload)
        except ServerRequestError as exc:
            self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/execute":
            response = collect_command_response(
                self.server.server_config,
                request,
                event_source=self.server.event_source,
            )
            self._write_json_response(HTTPStatus.OK, response)
            return

        if path == "/api/execute/stream":
            self._write_ndjson_response(
                self.server.event_source(self.server.server_config, request)
            )
            return

        self._write_json_response(
            HTTPStatus.NOT_FOUND,
            {"error": f"Unknown endpoint: {path}"},
        )

    def _read_json_payload(self) -> object:
        header_value = self.headers.get("Content-Length")
        if header_value is None:
            raise ServerRequestError("Missing Content-Length header.")

        try:
            content_length = int(header_value)
        except ValueError as exc:
            raise ServerRequestError("Invalid Content-Length header.") from exc

        if content_length <= 0:
            raise ServerRequestError("Request body must not be empty.")

        raw_body = self.rfile.read(content_length)
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ServerRequestError("Request body must contain valid JSON.") from exc

    def _send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")

    def _write_json_response(
        self,
        status: HTTPStatus,
        payload: object,
    ) -> None:
        body = _compact_json(payload)
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", _JSON_MIME)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _write_ndjson_response(self, events: Iterator[CommandEvent]) -> None:
        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        self.send_header("Content-Type", _NDJSON_MIME)
        self.end_headers()
        for event in events:
            self.wfile.write(_compact_json(event))
            self.wfile.write(b"\n")
            self.wfile.flush()


def create_http_server(
    config: ServerConfig,
    *,
    event_source: CommandEventSource = iter_command_events,
) -> MlodyApiServer:
    """Create the HTTP server without starting the event loop."""

    return MlodyApiServer(
        (config.http_host, config.http_port),
        config=config,
        event_source=event_source,
    )


def _run_lsp_tcp_server(config: ServerConfig) -> None:
    from mlody.lsp.server import configure_runtime_roots, server as lsp_server

    configure_runtime_roots(
        monorepo_root=config.monorepo_root,
        workspace_root=config.workspace_root,
        roots_file=config.roots,
        full_workspace=config.full_workspace,
    )
    lsp_server.start_tcp(config.lsp_host, config.lsp_port)


def _start_lsp_thread(
    config: ServerConfig,
    *,
    lsp_runner: Callable[[ServerConfig], None] = _run_lsp_tcp_server,
) -> threading.Thread:
    def _target() -> None:
        _logger.info(
            "Starting mlody LSP server on tcp://%s:%s",
            config.lsp_host,
            config.lsp_port,
        )
        lsp_runner(config)

    thread = threading.Thread(target=_target, name="mlody-lsp", daemon=True)
    thread.start()
    return thread


def run_server(
    config: ServerConfig,
    *,
    event_source: CommandEventSource = iter_command_events,
    lsp_runner: Callable[[ServerConfig], None] = _run_lsp_tcp_server,
) -> None:
    """Run the persistent HTTP+LSP server until interrupted."""

    http_server = create_http_server(config, event_source=event_source)
    _start_lsp_thread(config, lsp_runner=lsp_runner)

    click.echo(
        f"HTTP API listening on http://{config.http_host}:{http_server.server_port}"
    )
    click.echo(f"LSP listening on tcp://{config.lsp_host}:{config.lsp_port}")

    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("Shutting down mlody server")
    finally:
        http_server.shutdown()
        http_server.server_close()
