"""Persistent server mode for mlody CLI.

This module exposes two transports side-by-side:

* HTTP/JSON for browser and tool integrations.
* TCP LSP, reusing the existing pygls-based server from ``mlody.lsp``.

The HTTP API supports both one-shot JSON responses and streaming NDJSON so the
frontend can consume incremental state updates for long-running commands.
"""

from __future__ import annotations

import base64
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
import pyarrow as pa

from common.python.starlarkish.evaluator.evaluator import _runtime_json_data
from mlody.cli.show import (
    _describe_mlody_value,
    _display_payload,
    _parse_inner,
    _selected_show_user,
)
from mlody.core.derived import DerivedValueShapeError
from mlody.core.workspace_models import RootInfo
from mlody.core.label import parse_label
from mlody.core.label.label import Label
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular import source_from_value
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
from mlody.resolver.label_value import _RawAttrValue
from mlody.resolver.errors import WorkspaceResolutionError
from mlody.resolver.resolver import _workspace_injections, get_or_build_baseline_workspace

_logger = logging.getLogger(__name__)

_JSON_MIME = "application/json; charset=utf-8"
_NDJSON_MIME = "application/x-ndjson; charset=utf-8"
_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)


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


def _noop_print(*_args: object, **_kwargs: object) -> None:
    """Suppress incidental print output while loading workspace metadata."""


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


def parse_verbatim_command_request(payload: object) -> ServerCommandRequest:
    """Validate a raw command payload without shell-style splitting."""

    if not isinstance(payload, Mapping):
        raise ServerRequestError("Request body must be a JSON object.")

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ServerRequestError("Field 'command' must be a non-empty string.")
    normalized_command = command.strip()
    if normalized_command != "show":
        raise ServerRequestError(f"Unsupported command: {normalized_command}")

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

    target_text = input_text.strip()
    if target_text == "":
        raise ServerRequestError("Show requests require a target.")

    return ServerCommandRequest(
        request_id=normalized_request_id,
        command=normalized_command,
        arguments=(target_text,),
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


def _current_baseline_workspace(config: ServerConfig) -> object:
    """Return the loaded baseline workspace for the server's current roots."""
    extra_roots, lazy_roots = _workspace_injections(
        config.monorepo_root, config.workspace_root
    )
    return get_or_build_baseline_workspace(
        mode="cwd",
        monorepo_root=config.monorepo_root,
        workspace_root=config.workspace_root,
        roots_file=config.roots,
        full_workspace=config.full_workspace,
        print_fn=_noop_print,
        extra_roots=extra_roots,
        lazy_roots=lazy_roots,
        verbose=config.verbose,
    )


def _serialize_registered_users(workspace: object) -> list[dict[str, object]]:
    evaluator = getattr(workspace, "evaluator", None)
    registry = getattr(evaluator, "registry", None)
    users = getattr(registry, "users", None)
    by_name = getattr(users, "by_name", None)
    if not isinstance(by_name, Mapping):
        return []

    payloads: list[dict[str, object]] = []
    for raw_user in by_name.values():
        name = getattr(raw_user, "name", None)
        if not isinstance(name, str):
            continue
        payload = _runtime_json_data(raw_user)
        if isinstance(payload, dict):
            payloads.append(payload)
        else:
            payloads.append({"name": name})
    payloads.sort(key=lambda item: str(item.get("name", "")))
    return payloads


def _serialize_root_infos(workspace: object) -> list[dict[str, object]]:
    root_infos = getattr(workspace, "root_infos", {})
    if not isinstance(root_infos, Mapping):
        return []

    payloads: list[dict[str, object]] = []
    for root_name, raw_root_info in root_infos.items():
        if isinstance(raw_root_info, RootInfo):
            payloads.append(
                {
                    "name": raw_root_info.name,
                    "path": raw_root_info.path,
                    "description": raw_root_info.description,
                }
            )
            continue

        payload = _runtime_json_data(raw_root_info)
        if isinstance(payload, dict):
            if "name" not in payload and isinstance(root_name, str):
                payload = {**payload, "name": root_name}
            payloads.append(payload)

    payloads.sort(key=lambda item: str(item.get("name", "")))
    return payloads


def _serialize_workspace_context(workspace: object) -> dict[str, object]:
    evaluator = getattr(workspace, "evaluator", None)
    extra_ctx = getattr(evaluator, "_extra_ctx", None)
    payload: dict[str, object] = {}

    workspace_ctx = getattr(extra_ctx, "workspace", None)
    if workspace_ctx is not None:
        payload["workspace"] = _runtime_json_data(workspace_ctx)

    run_ctx = getattr(extra_ctx, "run", None)
    if run_ctx is not None:
        payload["run"] = _runtime_json_data(run_ctx)

    return payload


def _workspace_summary_payload(config: ServerConfig, workspace: object) -> dict[str, object]:
    return {
        "monorepoRoot": str(config.monorepo_root),
        "workspaceRoot": str(config.workspace_root),
        "rootsFile": str(config.roots) if config.roots is not None else None,
        "fullWorkspace": config.full_workspace,
        "info": _runtime_json_data(getattr(workspace, "info", None)),
        "rootInfos": _serialize_root_infos(workspace),
        "context": _serialize_workspace_context(workspace),
    }


def _stage_json_result(
    title: str,
    data: object,
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "json",
            "title": title,
        },
        "data": _stage_json_data(data),
    }


def _stage_table_result(
    title: str,
    *,
    column_names: Sequence[str],
    rows: Sequence[object],
    total_rows: int,
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "table",
            "title": title,
            "columns": [
                {
                    "key": column_name,
                    "label": column_name,
                }
                for column_name in column_names
            ],
            "rowCount": total_rows,
            "truncated": total_rows > len(rows),
        },
        "data": _stage_json_data(list(rows)),
    }


def _image_mime_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image/gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"BM"):
        return "image/bmp"
    return None


def _encoded_stage_image(value: object) -> dict[str, object] | None:
    raw: bytes | None = None
    path: str | None = None

    if isinstance(value, Mapping):
        raw_candidate = value.get("bytes")
        if isinstance(raw_candidate, bytes):
            raw = raw_candidate
        path_candidate = value.get("path")
        if isinstance(path_candidate, str) and path_candidate.strip():
            path = path_candidate
    elif isinstance(value, bytes):
        raw = value

    if not raw:
        return None

    mime_type = _image_mime_type(raw)
    if mime_type is None:
        return None

    payload: dict[str, object] = {
        "kind": "encoded-image",
        "mimeType": mime_type,
        "base64": base64.b64encode(raw).decode("ascii"),
        "byteLength": len(raw),
    }
    if path is not None:
        payload["path"] = path
    return payload


def _stage_json_data(obj: object, *, _seen: set[int] | None = None) -> object:
    image_payload = _encoded_stage_image(obj)
    if image_payload is not None:
        return image_payload

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if callable(obj) and not isinstance(obj, type):
        return "<callable>"
    if hasattr(obj, "as_mapping"):
        forced_obj = force(obj)  # type: ignore[arg-type]
        if hasattr(forced_obj, "as_mapping"):
            obj = forced_obj

    if _seen is None:
        _seen = set()

    is_container_like = hasattr(obj, "as_mapping") or isinstance(
        obj, (Mapping, list, tuple, set)
    )
    obj_id = id(obj)
    if is_container_like:
        if obj_id in _seen:
            return "<cycle>"
        _seen.add(obj_id)

    try:
        if hasattr(obj, "as_mapping"):
            return {
                str(key): _stage_json_data(value, _seen=_seen)
                for key, value in obj.as_mapping().items()
                if key not in {"raw", "_entity_type"}
            }
        if isinstance(obj, Mapping):
            return {
                str(key): _stage_json_data(value, _seen=_seen)
                for key, value in obj.items()
            }
        if isinstance(obj, (list, tuple, set)):
            return [_stage_json_data(value, _seen=_seen) for value in obj]
        return repr(obj)
    finally:
        if is_container_like:
            _seen.remove(obj_id)


def _stage_preview_from_rows(
    column_names: Sequence[str],
    rows: Sequence[Mapping[str, object]],
    *,
    total_rows: int,
) -> tuple[list[str], list[list[object]], int]:
    normalized_columns = [str(column_name) for column_name in column_names]
    normalized_rows: list[list[object]] = []
    for row in rows:
        normalized_rows.append(
            [_stage_json_data(row.get(column_name)) for column_name in normalized_columns]
        )
    return normalized_columns, normalized_rows, total_rows


def _stage_preview_from_pyarrow_table(
    table: pa.Table,
    *,
    total_rows: int,
) -> tuple[list[str], list[list[object]], int]:
    return _stage_preview_from_rows(
        table.column_names,
        cast(list[Mapping[str, object]], table.to_pylist()),
        total_rows=total_rows,
    )


def _dispatch_stage_value(
    workspace: object,
    dispatch_struct: object,
) -> dict[str, object] | None:
    from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415

    methods = list(
        workspace.evaluator._method_registry.get("stage_value", {}).get("methods", [])
    )
    if not methods:
        return None

    try:
        result = dispatch("stage_value", (dispatch_struct,), methods)
    except DispatchError:
        return None

    if result is None:
        return None
    return cast(dict[str, object], _stage_json_data(result))


def _raw_value_type_struct(workspace: object, value: _RawAttrValue) -> object | None:
    entity_query = getattr(value.label, "entity_query", None)
    entity = getattr(value.label, "entity", None)
    if entity_query is None or entity is None:
        return None

    from mlody.core.label.label import Label as _Label  # noqa: PLC0415

    base_label = _Label(
        workspace=value.label.workspace,
        workspace_query=value.label.workspace_query,
        entity=entity,
        entity_query=None,
        attribute_path=value.label.attribute_path,
        attribute_query=value.label.attribute_query,
    )
    try:
        base_value = workspace.resolve(base_label.format_inner())
    except Exception:
        return None
    return getattr(base_value, "type", None)


def _stage_dispatched_result(
    workspace: object,
    value: MlodyValue,
    *,
    title: str,
) -> dict[str, object] | None:
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    if isinstance(value, MlodyValueValue):
        display_payload = _display_payload(value)
        render_dispatch_value = value.struct
        if display_payload is not value.struct:
            if hasattr(display_payload, "as_mapping") and getattr(
                display_payload, "kind", None
            ) == "value":
                render_dispatch_value = display_payload
            else:
                render_dispatch_value = None

        if render_dispatch_value is None or not hasattr(display_payload, "as_mapping"):
            return None

        try:
            tabular_source = source_from_value(display_payload)
        except ValueError:
            return None

        try:
            preview = tabular_source.preview(50)
        except (
            DerivedValueShapeError,
            MlodyQueryError,
            TypeError,
            ValueError,
        ):
            return None
        dispatch_struct = Struct(
            **{
                **render_dispatch_value.as_mapping(),
                "_stage_preview": _stage_preview_from_pyarrow_table(
                    preview.table,
                    total_rows=preview.total_rows,
                ),
            }
        )
        return _dispatch_stage_value(workspace, dispatch_struct)

    if isinstance(value, _RawAttrValue):
        raw_value = value.value
        raw_preview: tuple[list[str], list[list[object]], int] | None = None

        if isinstance(raw_value, pa.Table):
            raw_preview = _stage_preview_from_pyarrow_table(
                raw_value,
                total_rows=raw_value.num_rows,
            )
        elif isinstance(raw_value, list) and raw_value and all(
            isinstance(row, Mapping) for row in raw_value
        ):
            column_names: list[str] = []
            for row in raw_value:
                for key in row:
                    key_text = str(key)
                    if key_text not in column_names:
                        column_names.append(key_text)
            raw_preview = _stage_preview_from_rows(
                column_names,
                cast(list[Mapping[str, object]], raw_value),
                total_rows=len(raw_value),
            )
        elif isinstance(raw_value, Mapping):
            column_names = [str(key) for key in raw_value.keys()]
            raw_preview = _stage_preview_from_rows(
                column_names,
                [cast(Mapping[str, object], raw_value)],
                total_rows=1,
            )

        if raw_preview is None:
            return None

        dispatch_kwargs: dict[str, object] = {
            "kind": "value",
            "name": title,
            "_stage_preview": raw_preview,
        }
        type_struct = _raw_value_type_struct(workspace, value)
        if type_struct is not None:
            dispatch_kwargs["type"] = type_struct
        dispatch_struct = Struct(**dispatch_kwargs)
        return _dispatch_stage_value(workspace, dispatch_struct)

    return None


def _stage_result_for_mlody_value(
    value: MlodyValue,
    *,
    title: str,
) -> dict[str, object]:
    if isinstance(value, _RawAttrValue):
        raw_value = value.value
        if isinstance(raw_value, list) and raw_value and all(
            isinstance(row, dict) for row in raw_value
        ):
            column_names: list[str] = []
            for row in raw_value:
                for key in row:
                    if key not in column_names:
                        column_names.append(str(key))
            return _stage_table_result(
                title,
                column_names=column_names,
                rows=raw_value,
                total_rows=len(raw_value),
            )
        if isinstance(raw_value, dict):
            return _stage_table_result(
                title,
                column_names=[str(key) for key in raw_value.keys()],
                rows=[raw_value],
                total_rows=1,
            )
        return _stage_json_result(title, raw_value)

    if isinstance(value, MlodyVectorValue):
        return _stage_json_result(
            title,
            [
                _stage_result_for_mlody_value(element, title=_describe_mlody_value(element))
                for element in value.elements
            ],
        )

    if isinstance(value, MlodyValueValue):
        display_payload = _display_payload(value)
        if hasattr(display_payload, "as_mapping"):
            try:
                tabular_source = source_from_value(display_payload)
            except ValueError:
                tabular_source = None
            if tabular_source is not None:
                try:
                    preview = tabular_source.preview(50)
                    return _stage_table_result(
                        title,
                        column_names=list(preview.table.column_names),
                        rows=preview.table.to_pylist(),
                        total_rows=preview.total_rows,
                    )
                except (
                    DerivedValueShapeError,
                    MlodyQueryError,
                    TypeError,
                    ValueError,
                ) as exc:
                    return _stage_json_result(
                        title,
                        {
                            "error": str(exc),
                            "value": _serialize_mlody_value(value),
                        },
                    )
        return _stage_json_result(title, display_payload)

    return _stage_json_result(title, _serialize_mlody_value(value))


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


def execute_verbatim_command_response(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> dict[str, object]:
    """Run the real Click show command and capture its textual output."""

    if request.command != "show":
        raise ServerRequestError(f"Unsupported command: {request.command}")

    from click.testing import CliRunner
    from mlody.cli.main import cli as root_cli

    config_overrides = _read_string_list_option(request.options, "config", "with")
    run_as = _read_string_option(request.options, "runAs", "run_as", default="mav")

    cli_args: list[str] = []
    if config.verbose:
        cli_args.append("--verbose")
    if config.full_workspace:
        cli_args.append("--full-workspace")
    cli_args.extend(["show"])
    for override in config_overrides:
        cli_args.extend(["--with", override])
    cli_args.extend(["--as", run_as])
    cli_args.extend(list(request.arguments))

    runner = CliRunner()
    result = runner.invoke(
        root_cli,
        cli_args,
        obj={
            "monorepo_root": config.monorepo_root,
            "roots": config.roots,
        },
        color=False,
        catch_exceptions=False,
    )

    return {
        "requestId": request.request_id,
        "command": request.command,
        "status": "done" if result.exit_code == 0 else "error",
        "exitCode": result.exit_code,
        "output": result.output,
    }


def execute_stage_command_response(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> dict[str, object]:
    """Resolve a raw show request into the stage JSON payload."""

    if request.command != "show":
        raise ServerRequestError(f"Unsupported command: {request.command}")

    config_overrides = _read_string_list_option(request.options, "config", "with")
    run_as = _read_string_option(request.options, "runAs", "run_as", default="mav")
    target = request.arguments[0]

    workspace, _resolved_sha = resolve_workspace(
        target,
        monorepo_root=config.monorepo_root,
        workspace_root=config.workspace_root,
        config=config_overrides,
        user=run_as,
        roots_file=config.roots,
        full_workspace=config.full_workspace,
        verbose=config.verbose,
    )
    committoid, inner_label = _parse_inner(target)

    stage_results: list[dict[str, object]] = []
    for expanded_inner in workspace.expand_wildcard_label(inner_label):
        full_label = f"{committoid}|{expanded_inner}" if committoid else expanded_inner
        concrete_label = _concrete_show_label(committoid, expanded_inner)
        mlody_value = resolve_label_to_value(concrete_label, workspace)
        if isinstance(mlody_value, MlodyUnresolvedValue):
            raise ServerRequestError(mlody_value.reason)
        stage_results.append(
            _stage_dispatched_result(
                workspace,
                mlody_value,
                title=full_label,
            )
            or _stage_result_for_mlody_value(mlody_value, title=full_label)
        )

    if len(stage_results) == 1:
        return stage_results[0]

    return _stage_json_result(target, stage_results)


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
        if path == "/healthz":
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
            return

        if path == "/api/users":
            try:
                workspace = _current_baseline_workspace(self.server.server_config)
                self._write_json_response(
                    HTTPStatus.OK,
                    _serialize_registered_users(workspace),
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load users API payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path == "/api/workspace":
            try:
                workspace = _current_baseline_workspace(self.server.server_config)
                self._write_json_response(
                    HTTPStatus.OK,
                    _workspace_summary_payload(self.server.server_config, workspace),
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load workspace API payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        self._write_json_response(
            HTTPStatus.NOT_FOUND,
            {"error": f"Unknown endpoint: {path}"},
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        try:
            payload = self._read_json_payload()
        except ServerRequestError as exc:
            self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/execute/verbatim":
            try:
                request = parse_verbatim_command_request(payload)
                response = execute_verbatim_command_response(
                    self.server.server_config,
                    request,
                )
                self._write_json_response(HTTPStatus.OK, response)
            except ServerRequestError as exc:
                self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to execute verbatim command")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path == "/api/execute/stage":
            try:
                request = parse_verbatim_command_request(payload)
                response = execute_stage_command_response(
                    self.server.server_config,
                    request,
                )
                self._write_json_response(HTTPStatus.OK, response)
            except ServerRequestError as exc:
                self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to execute stage command")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        try:
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
        try:
            self.send_response(status)
            self._send_common_headers()
            self.send_header("Content-Type", _JSON_MIME)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            _logger.debug("Client disconnected before JSON response completed.")

    def _write_ndjson_response(self, events: Iterator[CommandEvent]) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self._send_common_headers()
            self.send_header("Content-Type", _NDJSON_MIME)
            self.end_headers()
            for event in events:
                self.wfile.write(_compact_json(event))
                self.wfile.write(b"\n")
                self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            _logger.debug("Client disconnected before NDJSON response completed.")


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
