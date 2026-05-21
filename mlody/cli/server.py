"""Persistent server mode for mlody CLI.

This module exposes two transports side-by-side:

* HTTP/JSON for browser and tool integrations.
* TCP LSP, reusing the existing pygls-based server from ``mlody.lsp``.

The HTTP API supports both one-shot JSON responses and streaming NDJSON so the
frontend can consume incremental state updates for long-running commands.
"""

from __future__ import annotations

import base64
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import errno
import itertools
import json
import logging
import mimetypes
import os
import shlex
import socket
import subprocess
import threading
import time
from urllib.parse import unquote
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Any, cast

import click
import networkx
import pyarrow as pa

from common.python.starlarkish.evaluator.evaluator import _runtime_json_data
from mlody.cli.autocomplete import (
    StageAutocompleteRequest,
    parse_stage_autocomplete_request,
    stage_autocomplete_payload,
)
from mlody.cli.action_graph_render import build_stage_action_graph_data
from mlody.cli.asset_render import asset_metadata_payload
from mlody.cli.dag_render import build_stage_dag_data
from mlody.cli.lineage_render import is_lineage_type, lineage_rows_from_payload
from mlody.cli.show_execution import PreparedShowValue, execute_show_action_graph
from mlody.cli.show import (
    _action_graph_title_for_value,
    _dag_title_for_value,
    _describe_mlody_value,
    _display_payload,
    _is_action_graph_value,
    _is_dag_value,
    _parse_inner,
    _selected_show_user,
)
from mlody.core.derived import DerivedValueShapeError
from mlody.core.workspace_models import RootInfo
from mlody.core.label import parse_label
from mlody.core.label.label import Label
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular.location_specs import source_from_value
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
from mlody.resolver.entity_summary import summarize_action_struct, summarize_task_struct
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.structural import MlodySourceRangeValue
from mlody.resolver.errors import WorkspaceResolutionError
from mlody.resolver.resolver import (
    Reporter,
    _make_workspace_request,
    _workspace_injections,
    get_or_build_baseline_workspace,
)

_logger = logging.getLogger(__name__)

_JSON_MIME = "application/json; charset=utf-8"
_NDJSON_MIME = "application/x-ndjson; charset=utf-8"
_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)
_STATIC_TEXT_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
_RUNFILES_MANIFEST: dict[str, Path] | None = None
_MAX_STAGE_REQUEST_LOGS = 200
_STAGE_QUERY_LIST_ENTITIES = frozenset(
    {"teams", "users", "tasks", "types", "locations", "values"}
)

_RESTART_WATCHER_SCRIPT = """
import json
import os
import subprocess
import sys
import time

parent_pid = int(sys.argv[1])
cwd = sys.argv[2]
argv = json.loads(sys.argv[3])

while True:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        break
    time.sleep(0.1)

subprocess.Popen(argv, cwd=cwd, close_fds=True, start_new_session=True)
"""


def _default_restart_argv() -> tuple[str, ...]:
    original_argv = tuple(getattr(sys, "orig_argv", ()) or ())
    if original_argv:
        return original_argv
    return (sys.executable, *sys.argv)


def _spawn_restart_watcher(
    config: "ServerConfig",
    *,
    parent_pid: int | None = None,
) -> None:
    effective_parent_pid = os.getpid() if parent_pid is None else parent_pid
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            _RESTART_WATCHER_SCRIPT,
            str(effective_parent_pid),
            str(config.restart_cwd),
            json.dumps(list(config.restart_argv)),
        ],
        cwd=str(config.restart_cwd),
        env=os.environ.copy(),
        close_fds=True,
        start_new_session=True,
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
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_monotonic: float = field(default_factory=time.monotonic)
    restart_cwd: Path = field(default_factory=Path.cwd)
    restart_argv: tuple[str, ...] = field(default_factory=_default_restart_argv)
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ServerCommandRequest:
    """Normalized command request accepted by the HTTP API."""

    request_id: str
    command: str
    arguments: tuple[str, ...]
    options: dict[str, object]
    input_text: str = ""


@dataclass(frozen=True)
class StageQueryListRequest:
    """Validated stage query/list request payload."""

    entity: str
    workspace_root: str | None


class ServerRequestError(ValueError):
    """Raised when an incoming HTTP request cannot be parsed or validated."""


CommandEvent = dict[str, object]
CommandEventSource = Callable[[ServerConfig, ServerCommandRequest], Iterator[CommandEvent]]


def _compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _noop_print(*_args: object, **_kwargs: object) -> None:
    """Suppress incidental print output while loading workspace metadata."""


def _stage_static_roots() -> tuple[Path, Path]:
    """Return the runfiles-visible roots for the stage app and avatar assets."""

    runfiles_root = _runfiles_root()
    if runfiles_root is not None:
        stage_root = runfiles_root / "_main" / "mlody" / "stage"
        avatar_root = runfiles_root / "_main" / "mlody" / "assets" / "images" / "avatars"
        return stage_root, avatar_root

    mlody_root = Path(__file__).resolve().parents[1]
    return (mlody_root / "stage", mlody_root / "assets" / "images" / "avatars")


def _runfiles_root() -> Path | None:
    candidates: list[Path] = []

    env_dir = os.environ.get("RUNFILES_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    argv0 = Path(sys.argv[0]).resolve()
    candidates.append(argv0.parent / f"{argv0.name}.runfiles")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _runfiles_manifest_path() -> Path | None:
    env_manifest = os.environ.get("RUNFILES_MANIFEST_FILE")
    if env_manifest:
        candidate = Path(env_manifest)
        if candidate.is_file():
            return candidate

    runfiles_root = _runfiles_root()
    if runfiles_root is not None:
        manifest = runfiles_root / "MANIFEST"
        if manifest.is_file():
            return manifest

    argv0 = Path(sys.argv[0]).resolve()
    adjacent_manifest = argv0.parent / f"{argv0.name}.runfiles_manifest"
    if adjacent_manifest.is_file():
        return adjacent_manifest

    return None


def _runfiles_manifest() -> Mapping[str, Path]:
    global _RUNFILES_MANIFEST
    if _RUNFILES_MANIFEST is not None:
        return _RUNFILES_MANIFEST

    manifest_path = _runfiles_manifest_path()
    if manifest_path is None:
        _RUNFILES_MANIFEST = {}
        return _RUNFILES_MANIFEST

    mapping: dict[str, Path] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        logical, separator, physical = line.partition(" ")
        if not separator or not physical:
            continue
        mapping[logical] = Path(physical)
    _RUNFILES_MANIFEST = mapping
    return _RUNFILES_MANIFEST


def _runfiles_path(logical_path: str) -> Path | None:
    mapped = _runfiles_manifest().get(logical_path)
    if mapped is not None and mapped.is_file():
        return mapped
    return None


def _resolve_stage_static_asset(request_path: str) -> Path | None:
    """Map a browser path to a stage static asset or SPA entrypoint."""

    stage_root, avatar_root = _stage_static_roots()
    raw_relative = unquote(request_path.lstrip("/"))
    if raw_relative == "":
        return _resolve_stage_static_logical_asset("index.html")

    relative_path = Path(raw_relative)
    if relative_path.is_absolute() or any(part in {"..", ""} for part in relative_path.parts):
        return None

    if relative_path.parts[:3] == ("assets", "images", "avatars"):
        avatar_relative = Path(*relative_path.parts[3:])
        avatar_candidate = avatar_root / avatar_relative
        if avatar_candidate.is_file():
            return avatar_candidate
        return _runfiles_path(
            f"_main/mlody/assets/images/avatars/{avatar_relative.as_posix()}"
        )

    candidate = stage_root / relative_path
    if candidate.is_file():
        return candidate
    manifest_candidate = _resolve_stage_static_logical_asset(relative_path.as_posix())
    if manifest_candidate is not None:
        return manifest_candidate

    if candidate.suffix == "":
        return _resolve_stage_static_logical_asset("index.html")

    return None


def _resolve_stage_static_logical_asset(relative_path: str) -> Path | None:
    stage_root, _avatar_root = _stage_static_roots()
    direct_candidate = stage_root / relative_path
    if direct_candidate.is_file():
        return direct_candidate
    return _runfiles_path(f"_main/mlody/stage/{relative_path}")


def _static_content_type(path: Path) -> str:
    """Return the HTTP content-type for a static stage asset."""

    suffix = path.suffix.lower()
    if suffix in _STATIC_TEXT_CONTENT_TYPES:
        return _STATIC_TEXT_CONTENT_TYPES[suffix]
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


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


def _log_event_timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()


def _json_log_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_log_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_json_log_value(item) for item in value]
    try:
        return _runtime_json_data(value)
    except Exception:  # noqa: BLE001
        return str(value)


def _structured_log_payload(record: logging.LogRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "log",
        "timestamp": _log_event_timestamp(record),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    if isinstance(record.msg, str):
        payload["template"] = record.msg
    if record.args:
        if isinstance(record.args, Mapping):
            payload["values"] = {
                str(key): _json_log_value(value)
                for key, value in record.args.items()
            }
        elif isinstance(record.args, tuple):
            payload["values"] = [_json_log_value(value) for value in record.args]
        else:
            payload["values"] = _json_log_value(record.args)
    if record.exc_info:
        exception_text = logging.Formatter().formatException(record.exc_info)
        payload["exception"] = exception_text
        payload["message"] = f"{payload['message']}\n{exception_text}"
    return payload


class _ThreadScopedStructuredLogHandler(logging.Handler):
    """Collect logger output emitted on the current thread as structured events."""

    def __init__(
        self,
        *,
        request_id: str,
        next_sequence: Callable[[], int],
        sink: list[CommandEvent],
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._request_id = request_id
        self._next_sequence = next_sequence
        self._sink = sink
        self._thread_id = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if threading.get_ident() != self._thread_id:
            return
        payload = _structured_log_payload(record)
        payload["requestId"] = self._request_id
        payload["sequence"] = self._next_sequence()
        self._sink.append(cast(CommandEvent, payload))


def _completion_status(*, result_count: int, error_count: int) -> str:
    if error_count == 0:
        return "done"
    if result_count > 0:
        return "partial_error"
    return "error"


def _history_file_path() -> Path:
    return Path.home() / ".cache" / "mlody" / "history.json"


def _config_for_workspace_root(
    config: ServerConfig,
    workspace_root: Path,
) -> ServerConfig:
    return replace(config, workspace_root=workspace_root)


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


def _workspace_root_to_relative(workspace_root: Path, monorepo_root: Path) -> str:
    """Encode *workspace_root* as a path relative to *monorepo_root*.

    Returns ``""`` when *workspace_root* is the monorepo root itself, or a
    POSIX-style relative path otherwise. Both inputs must already lie inside
    the same monorepo — the caller is responsible for that invariant.
    """
    relative = workspace_root.resolve().relative_to(monorepo_root.resolve())
    relative_str = relative.as_posix()
    return "" if relative_str == "." else relative_str


def _workspace_root_from_request(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> Path:
    """Resolve ``workspaceRoot`` to an absolute path inside the monorepo.

    The wire protocol carries ``workspaceRoot`` as a path relative to the
    server's monorepo root (``""`` denotes the monorepo root itself). Absolute
    paths and any ``..`` escape are rejected. A missing or ``null`` option
    falls back to the server's launch-time workspace root.
    """
    raw = request.options.get("workspaceRoot", request.options.get("workspace_root"))
    if raw is None:
        return config.workspace_root.resolve()
    if not isinstance(raw, str):
        raise ServerRequestError("Workspace root must be a string or null.")

    raw_workspace_root = raw.strip()
    if raw_workspace_root == "":
        return config.monorepo_root.resolve()

    candidate = Path(raw_workspace_root)
    if candidate.is_absolute() or candidate.expanduser() != candidate:
        raise ServerRequestError(
            "Workspace root must be relative to the monorepo root."
        )
    if ".." in candidate.parts:
        raise ServerRequestError(
            "Workspace root must not escape the monorepo root."
        )

    monorepo_root = config.monorepo_root.resolve()
    resolved = (monorepo_root / candidate).resolve()
    try:
        resolved.relative_to(monorepo_root)
    except ValueError as exc:
        raise ServerRequestError(
            "Workspace root must stay within the current monorepo."
        ) from exc

    if not resolved.exists():
        raise ServerRequestError(
            f"Workspace root does not exist: {raw_workspace_root}"
        )
    if not resolved.is_dir():
        raise ServerRequestError(
            f"Workspace root is not a directory: {raw_workspace_root}"
        )

    return resolved


def _available_workspace_roots(config: ServerConfig) -> tuple[Path, ...]:
    monorepo_root = config.monorepo_root.resolve()
    current_workspace_root = config.workspace_root.resolve()
    candidates: set[Path] = {monorepo_root, current_workspace_root}

    for workspace_file in monorepo_root.rglob("workspace.mlody"):
        try:
            workspace_root = workspace_file.parent.resolve()
            workspace_root.relative_to(monorepo_root)
        except ValueError:
            continue
        candidates.add(workspace_root)

    def _sort_key(workspace_root: Path) -> tuple[int, str]:
        if workspace_root == current_workspace_root:
            return (0, ".")
        if workspace_root == monorepo_root:
            return (1, ".")
        return (2, str(workspace_root.relative_to(monorepo_root)))

    return tuple(sorted(candidates, key=_sort_key))


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
    for alias in (
        "runAs",
        "run_as",
        "config",
        "with",
        "workspaceRoot",
        "workspace_root",
    ):
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
    for alias in (
        "runAs",
        "run_as",
        "config",
        "with",
        "workspaceRoot",
        "workspace_root",
    ):
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


def parse_stage_query_list_request(payload: object) -> StageQueryListRequest:
    """Validate the stage query/list HTTP payload."""

    if not isinstance(payload, Mapping):
        raise ServerRequestError("Request body must be a JSON object.")

    entity = payload.get("entity")
    if not isinstance(entity, str) or not entity.strip():
        raise ServerRequestError("Field 'entity' must be a non-empty string.")
    normalized_entity = entity.strip().lower()
    if normalized_entity not in _STAGE_QUERY_LIST_ENTITIES:
        supported_entities = ", ".join(sorted(_STAGE_QUERY_LIST_ENTITIES))
        raise ServerRequestError(
            f"Field 'entity' must be one of: {supported_entities}."
        )

    workspace_root = payload.get("workspaceRoot")
    if workspace_root is not None and not isinstance(workspace_root, str):
        raise ServerRequestError("Field 'workspaceRoot' must be a string or null.")

    return StageQueryListRequest(
        entity=normalized_entity,
        workspace_root=workspace_root,
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
    request = _make_workspace_request(
        mode="cwd",
        monorepo_root=config.monorepo_root,
        workspace_root=config.workspace_root,
        roots_file=config.roots,
        full_workspace=config.full_workspace,
        print_fn=_noop_print,
        extra_roots=extra_roots,
        lazy_roots=lazy_roots,
    )
    reporter = Reporter(print_fn=_noop_print, verbose=config.verbose)
    return get_or_build_baseline_workspace(request, reporter)


def _baseline_workspace_for_root(
    config: ServerConfig,
    workspace_root: Path,
) -> object:
    return _current_baseline_workspace(
        _config_for_workspace_root(config, workspace_root)
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


def _workspace_summary_payload(
    config: ServerConfig,
    workspace: object,
    *,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    effective_workspace_root = workspace_root or config.workspace_root
    return {
        "workspaceRoot": _workspace_root_to_relative(
            effective_workspace_root, config.monorepo_root
        ),
        "rootsFile": str(config.roots) if config.roots is not None else None,
        "fullWorkspace": config.full_workspace,
        "info": _runtime_json_data(getattr(workspace, "info", None)),
        "rootInfos": _serialize_root_infos(workspace),
        "context": _serialize_workspace_context(workspace),
    }


def _default_workspace_summary_payload(
    config: ServerConfig,
    *,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    effective_workspace_root = workspace_root or config.workspace_root
    return {
        "workspaceRoot": _workspace_root_to_relative(
            effective_workspace_root, config.monorepo_root
        ),
        "rootsFile": str(config.roots) if config.roots is not None else None,
        "fullWorkspace": config.full_workspace,
        "info": None,
        "rootInfos": [],
        "context": {},
    }


def _command_history_input_text(request: ServerCommandRequest) -> str:
    if request.input_text.strip():
        return request.input_text.strip()
    return " ".join(argument.strip() for argument in request.arguments if argument.strip())


def _request_run_as(request: ServerCommandRequest) -> str:
    return _read_string_option(request.options, "runAs", "run_as", default="mav")


def _extract_next_history_segment(
    value: str,
) -> tuple[list[str], str] | None:
    if value == "//":
        return ["//"], ""

    if value.startswith("//"):
        return ["//"], value[2:]

    if value == "...":
        return ["..."], ""

    if value.startswith("...//"):
        return ["...", "//"], value[5:]

    if value.startswith(".../"):
        return ["..."], value[4:]

    if value.startswith("...:"):
        return ["...:"], value[4:]

    if not value:
        return None

    index = 0
    if value.startswith("@"):
        index = 1

    while index < len(value):
        character = value[index]
        if character.isalnum() or character in {"_", "-"}:
            index += 1
            continue
        break

    if index == 0 or (value.startswith("@") and index == 1):
        return None

    promoted_segment = value[:index]
    rest = value[index:]

    if rest.startswith("//"):
        return [promoted_segment, "//"], rest[2:]

    if rest.startswith("/"):
        return [promoted_segment], rest[1:]

    if rest.startswith(":"):
        return [f"{promoted_segment}:"], rest[1:]

    if rest.startswith("."):
        return [promoted_segment], rest

    return None


def _history_prompt_and_breadcrumb(
    request: ServerCommandRequest,
) -> tuple[list[str], str]:
    input_text = _command_history_input_text(request)
    if request.command != "show" or len(request.arguments) != 1:
        return [], input_text

    remainder = input_text
    breadcrumb: list[str] = []
    while True:
        split_result = _extract_next_history_segment(remainder)
        if split_result is None:
            break
        promoted_segments, remainder = split_result
        breadcrumb.extend(promoted_segments)

    return breadcrumb, remainder


def _load_history_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _logger.exception("Failed to read command history from %s", path)
        return []

    if not isinstance(payload, list):
        return []

    return [entry for entry in payload if isinstance(entry, dict)]


def _append_history_entry(config: ServerConfig, request: ServerCommandRequest) -> None:
    history_path = _history_file_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_root = _workspace_root_from_request(config, request)
    request_config = _config_for_workspace_root(config, workspace_root)

    try:
        workspace = _current_baseline_workspace(request_config)
        workspace_payload = _workspace_summary_payload(
            request_config,
            workspace,
            workspace_root=workspace_root,
        )
    except Exception:  # noqa: BLE001
        _logger.exception("Failed to load workspace summary for command history")
        workspace_payload = _default_workspace_summary_payload(
            request_config,
            workspace_root=workspace_root,
        )

    breadcrumb, prompt = _history_prompt_and_breadcrumb(request)
    current_user_name = _request_run_as(request)
    entries = _load_history_entries(history_path)
    entries.append(
        {
            "id": uuid.uuid4().hex,
            "createdAt": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "command": request.command,
            "prompt": prompt,
            "breadcrumb": breadcrumb,
            "currentUserName": current_user_name,
            "workspace": workspace_payload,
        }
    )
    history_path.write_text(json.dumps(entries), encoding="utf-8")


def _record_history_entry(config: ServerConfig, request: ServerCommandRequest) -> None:
    try:
        _append_history_entry(config, request)
    except Exception:  # noqa: BLE001
        _logger.exception("Failed to persist command history")


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


def _stage_result_list_result(
    title: str,
    results: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "result-list",
            "title": title,
            "rowCount": len(results),
        },
        "data": results,
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


def _stage_query_list_result(
    title: str,
    rows: Sequence[object],
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "query-list",
            "title": title,
            "columns": [
                {"key": "name", "label": "Name"},
                {"key": "description", "label": "Description"},
            ],
            "rowCount": len(rows),
            "truncated": False,
        },
        "data": _stage_json_data(list(rows)),
    }


def _stage_lineage_result(
    title: str,
    rows: Sequence[object],
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "lineage",
            "title": title,
            "rowCount": len(rows),
        },
        "data": [
            {
                "source": getattr(row, "source"),
                "value": _stage_json_data(getattr(row, "value")),
                "details": _stage_json_data(getattr(row, "details")),
                "active": bool(getattr(row, "active")),
            }
            for row in rows
        ],
    }


def _stage_query_entity_description(entity: object) -> str | None:
    raw_description = getattr(entity, "description", None)
    if isinstance(raw_description, str):
        description = raw_description.strip()
        if description:
            return description

    payload = _runtime_json_data(entity)
    if isinstance(payload, Mapping):
        fallback_description = payload.get("description")
        if isinstance(fallback_description, str):
            description = fallback_description.strip()
            if description:
                return description

    return None


def _iter_workspace_registry_rows(
    workspace: object,
) -> tuple[tuple[str, str, str, object], ...]:
    registry_view = getattr(workspace, "registry_view", None)
    iter_registry_items = getattr(registry_view, "iter_registry_items", None)
    if callable(iter_registry_items):
        raw_items = iter_registry_items()
    else:
        registry = getattr(getattr(workspace, "evaluator", None), "registry", None)
        all_items = getattr(registry, "all", None)
        raw_items = all_items.items() if isinstance(all_items, Mapping) else ()

    rows: list[tuple[str, str, str, object]] = []
    for raw_key, value in raw_items:
        if not isinstance(raw_key, tuple) or len(raw_key) != 3:
            continue
        raw_kind, raw_stem, raw_name = raw_key
        if (
            not isinstance(raw_kind, str)
            or not isinstance(raw_stem, str)
            or not isinstance(raw_name, str)
        ):
            continue
        rows.append((raw_kind, raw_stem, raw_name, value))
    return tuple(rows)


def _display_stage_query_name(
    *,
    stem: str,
    name: str,
    duplicate_names: set[str],
) -> str:
    if name not in duplicate_names or stem == "":
        return name
    return f"//{stem}:{name}"


def _stage_query_rows_for_kind(
    workspace: object,
    *,
    kind: str,
) -> list[dict[str, object]]:
    matching_rows = [
        (stem, name, value)
        for raw_kind, stem, name, value in _iter_workspace_registry_rows(workspace)
        if raw_kind == kind
    ]
    duplicate_names = {
        name for name, count in Counter(name for _stem, name, _value in matching_rows).items() if count > 1
    }

    rows: list[dict[str, object]] = []
    for stem, name, value in sorted(matching_rows, key=lambda row: (row[1], row[0])):
        row: dict[str, object] = {
            "name": _display_stage_query_name(
                stem=stem,
                name=name,
                duplicate_names=duplicate_names,
            )
        }
        description = _stage_query_entity_description(value)
        if description is not None:
            row["description"] = description
        rows.append(row)
    return rows


def _is_team_root_path(path: str) -> bool:
    normalized_path = path.lstrip("/")
    return normalized_path.startswith("mlody/teams/") or normalized_path.startswith(
        "teams/"
    )


def _stage_query_team_rows(workspace: object) -> list[dict[str, object]]:
    root_infos = getattr(workspace, "root_infos", None)
    if not isinstance(root_infos, Mapping):
        return []

    rows: list[dict[str, object]] = []
    for raw_name, root_info in root_infos.items():
        name = raw_name if isinstance(raw_name, str) else getattr(root_info, "name", None)
        path = getattr(root_info, "path", None)
        if not isinstance(name, str) or not isinstance(path, str):
            continue
        if not _is_team_root_path(path):
            continue
        row: dict[str, object] = {"name": name}
        description = _stage_query_entity_description(root_info)
        if description is not None:
            row["description"] = description
        rows.append(row)

    rows.sort(key=lambda row: cast(str, row["name"]))
    return rows


def _stage_query_list_title(entity: str) -> str:
    if entity == "values":
        return "Top-level Values"
    return entity.replace("-", " ").title()


def execute_stage_query_list_response(
    config: ServerConfig,
    request: StageQueryListRequest,
) -> dict[str, object]:
    workspace_root_request = ServerCommandRequest(
        request_id="stage-query-list",
        command="show",
        arguments=("@query//list",),
        options=(
            {"workspaceRoot": request.workspace_root}
            if request.workspace_root is not None
            else {}
        ),
    )
    workspace_root = _workspace_root_from_request(config, workspace_root_request)
    workspace = _baseline_workspace_for_root(config, workspace_root)

    if request.entity == "teams":
        rows = _stage_query_team_rows(workspace)
    else:
        registry_kind = {
            "users": "user",
            "tasks": "task",
            "types": "type",
            "locations": "location",
            "values": "value",
        }[request.entity]
        rows = _stage_query_rows_for_kind(workspace, kind=registry_kind)

    return _stage_query_list_result(_stage_query_list_title(request.entity), rows)


def _stage_dag_result(
    title: str,
    graph: networkx.MultiDiGraph,
) -> dict[str, object]:
    dag_data = build_stage_dag_data(graph)
    nodes = cast(list[object], dag_data["nodes"])
    edges = cast(list[object], dag_data["edges"])
    return {
        "kind": "result",
        "view": {
            "type": "dag",
            "title": title,
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
        },
        "data": dag_data,
    }


def _stage_action_graph_result(
    title: str,
    graph: networkx.DiGraph,
) -> dict[str, object]:
    action_graph_data = build_stage_action_graph_data(graph)
    nodes = cast(list[object], action_graph_data["nodes"])
    edges = cast(list[object], action_graph_data["edges"])
    return {
        "kind": "result",
        "view": {
            "type": "action-graph",
            "title": title,
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
        },
        "data": action_graph_data,
    }


def _stage_source_code_result(
    title: str,
    value: MlodySourceRangeValue,
) -> dict[str, object]:
    start_line = min(value.start_line, value.end_line)
    end_line = max(value.start_line, value.end_line)

    try:
        lines = value.abs_path.read_text().splitlines()
        code = "\n".join(lines[start_line - 1 : end_line])
    except Exception:
        code = f"# could not read {value.abs_path}"

    return {
        "kind": "result",
        "view": {
            "type": "source-code",
            "title": title,
        },
        "data": {
            "path": value.filepath,
            "language": "python",
            "startLine": start_line,
            "endLine": end_line,
            "code": code,
        },
    }


def _stage_task_result(
    title: str,
    value: MlodyTaskValue,
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "task",
            "title": title,
        },
        "data": summarize_task_struct(value.struct),
    }


def _stage_action_result(
    title: str,
    value: MlodyActionValue,
) -> dict[str, object]:
    return {
        "kind": "result",
        "view": {
            "type": "action",
            "title": title,
        },
        "data": summarize_action_struct(value.struct),
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


def _stage_value_type_struct(workspace: object, value: MlodyValue) -> object | None:
    if isinstance(value, _RawAttrValue):
        return _raw_value_type_struct(workspace, value)

    if isinstance(value, (MlodyTaskValue, MlodyActionValue, MlodyValueValue)):
        return getattr(value.struct, "type", None)

    return None


def _attach_stage_value_type(
    result: dict[str, object],
    *,
    workspace: object,
    value: MlodyValue,
) -> dict[str, object]:
    type_struct = _stage_value_type_struct(workspace, value)
    if type_struct is None:
        return result

    return {
        **result,
        "valueType": _stage_json_data(type_struct),
    }


def _attach_stage_request_id(
    result: dict[str, object],
    *,
    request_id: str,
) -> dict[str, object]:
    return {
        **result,
        "requestId": request_id,
    }


def _stage_result_for_resolved_value(
    workspace: object,
    value: MlodyValue,
    *,
    title: str,
    prepared: PreparedShowValue | None = None,
) -> dict[str, object]:
    stage_result = _stage_dispatched_result(
        workspace,
        value,
        title=title,
        prepared=prepared,
    )
    if stage_result is None:
        stage_result = _stage_result_for_mlody_value(
            value,
            title=title,
            prepared=prepared,
        )
    return _attach_stage_value_type(
        stage_result,
        workspace=workspace,
        value=value,
    )


def _stage_dispatched_result(
    workspace: object,
    value: MlodyValue,
    *,
    title: str,
    prepared: PreparedShowValue | None = None,
) -> dict[str, object] | None:
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    if isinstance(value, MlodyValueValue):
        if _is_dag_value(value):
            graph = (
                prepared.display_payload
                if prepared is not None
                else _display_payload(value)
            )
            if isinstance(graph, networkx.MultiDiGraph):
                return _stage_dag_result(_dag_title_for_value(value), graph)
            return None
        if _is_action_graph_value(value):
            graph = (
                prepared.display_payload
                if prepared is not None
                else _display_payload(value)
            )
            if isinstance(graph, networkx.DiGraph):
                return _stage_action_graph_result(
                    _action_graph_title_for_value(value),
                    graph,
                )
            return None

        display_payload = (
            prepared.display_payload
            if prepared is not None
            else _display_payload(value)
        )
        if is_lineage_type(getattr(value.struct, "type", None)):
            lineage_rows = lineage_rows_from_payload(display_payload)
            if lineage_rows is not None:
                return _stage_lineage_result(title, lineage_rows)
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

        if prepared is not None:
            if prepared.preview_table is None:
                return None
            preview_table = prepared.preview_table
            preview_total_rows = prepared.preview_total_rows or prepared.preview_table.num_rows
        else:
            try:
                tabular_source = source_from_value(display_payload)
            except ValueError:
                return None
            if tabular_source is None:
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
            preview_table = preview.table
            preview_total_rows = preview.total_rows

        dispatch_struct = Struct(
            **{
                **render_dispatch_value.as_mapping(),
                "_stage_preview": _stage_preview_from_pyarrow_table(
                    preview_table,
                    total_rows=preview_total_rows,
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
    prepared: PreparedShowValue | None = None,
) -> dict[str, object]:
    if isinstance(value, MlodyTaskValue):
        return _stage_task_result(title, value)
    if isinstance(value, MlodyActionValue):
        return _stage_action_result(title, value)

    if isinstance(value, MlodySourceRangeValue):
        return _stage_source_code_result(title, value)

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
        prepared_children = prepared.children if prepared is not None else ()
        return _stage_result_list_result(
            title,
            [
                _stage_result_for_mlody_value(
                    element,
                    title=(
                        ""
                        if isinstance(element, (MlodyTaskValue, MlodyActionValue))
                        else _describe_mlody_value(element)
                    ),
                    prepared=(
                        prepared_children[index]
                        if index < len(prepared_children)
                        else None
                    ),
                )
                for index, element in enumerate(value.elements)
            ],
        )

    if isinstance(value, MlodyValueValue):
        display_payload = (
            prepared.display_payload
            if prepared is not None
            else _display_payload(value)
        )
        if prepared is not None:
            if prepared.preview_table is not None:
                return _stage_table_result(
                    title,
                    column_names=list(prepared.preview_table.column_names),
                    rows=prepared.preview_table.to_pylist(),
                    total_rows=prepared.preview_total_rows
                    or prepared.preview_table.num_rows,
                )
            if prepared.preview_failure is not None and prepared.preview_failure.fatal:
                return _stage_json_result(
                    title,
                    {
                        "error": prepared.preview_failure.message,
                        "value": _serialize_mlody_value(value),
                    },
                )
        elif hasattr(display_payload, "as_mapping"):
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
        asset_payload = asset_metadata_payload(display_payload)
        if asset_payload is not None:
            return _stage_json_result(title, asset_payload)
        return _stage_json_result(title, display_payload)

    return _stage_json_result(title, _serialize_mlody_value(value))


def _execute_show_command(
    config: ServerConfig,
    request: ServerCommandRequest,
) -> Iterator[CommandEvent]:
    request_config = _config_for_workspace_root(
        config,
        _workspace_root_from_request(config, request),
    )
    config_overrides = _read_string_list_option(request.options, "config", "with")
    run_as = _request_run_as(request)

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
                monorepo_root=request_config.monorepo_root,
                workspace_root=request_config.workspace_root,
                config=config_overrides,
                user=run_as,
                roots_file=request_config.roots,
                full_workspace=request_config.full_workspace,
                verbose=request_config.verbose,
            )
            selected_user = _selected_show_user(workspace, run_as)
            committoid, inner_label = _parse_inner(target)

            for expanded_inner in workspace.expand_wildcard_label(inner_label):
                full_label = (
                    f"{committoid}|{expanded_inner}" if committoid else expanded_inner
                )
                concrete_label = _concrete_show_label(committoid, expanded_inner)
                execution = execute_show_action_graph(
                    workspace,
                    expanded_inner,
                    concrete_label,
                    resolve_label=resolve_label_to_value,
                    display_value=_display_payload,
                )
                mlody_value = execution.prepared_value.value
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
                    stageResult=_stage_result_for_resolved_value(
                        workspace,
                        mlody_value,
                        title=full_label,
                        prepared=execution.prepared_value,
                    ),
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
    run_as = _request_run_as(request)
    workspace_root = _workspace_root_from_request(config, request)

    cli_args: list[str] = []
    if config.verbose:
        cli_args.append("--verbose")
    if config.full_workspace:
        cli_args.append("--full-workspace")
    if workspace_root != config.monorepo_root:
        cli_args.extend(["--workspace", str(workspace_root)])
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


def _collect_stage_command_response(
    config: ServerConfig,
    request: ServerCommandRequest,
    *,
    event_source: CommandEventSource = iter_command_events,
) -> tuple[dict[str, object], list[CommandEvent]]:
    sequencer = itertools.count()
    log_events: list[CommandEvent] = []
    log_handler = _ThreadScopedStructuredLogHandler(
        request_id=request.request_id,
        next_sequence=lambda: next(sequencer),
        sink=log_events,
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    try:
        command_events: list[CommandEvent] = []
        for event in event_source(config, request):
            event_payload = dict(event)
            event_payload["sequence"] = next(sequencer)
            command_events.append(cast(CommandEvent, event_payload))
    finally:
        root_logger.removeHandler(log_handler)

    events = sorted(
        [*command_events, *log_events],
        key=lambda event: cast(int, event["sequence"]),
    )
    error_events = [
        event for event in command_events if event.get("event") == "error"
    ]
    if error_events:
        first_error = error_events[0]
        message = first_error.get("message")
        raise ServerRequestError(
            str(message) if isinstance(message, str) else "Show command failed."
        )

    stage_results: list[dict[str, object]] = []
    for event in [
        event for event in command_events if event.get("event") == "result"
    ]:
        stage_result = event.get("stageResult")
        if not isinstance(stage_result, Mapping):
            raise ServerRequestError("Show command produced no stage result payload.")
        stage_results.append(dict(stage_result))

    if not stage_results:
        raise ServerRequestError("Show command produced no results.")

    if len(stage_results) == 1:
        return (
            _attach_stage_request_id(stage_results[0], request_id=request.request_id),
            events,
        )

    return (
        _attach_stage_request_id(
            _stage_result_list_result(request.arguments[0], stage_results),
            request_id=request.request_id,
        ),
        events,
    )


def execute_stage_command_response(
    config: ServerConfig,
    request: ServerCommandRequest,
    *,
    event_source: CommandEventSource = iter_command_events,
) -> dict[str, object]:
    """Resolve a raw show request into the stage JSON payload."""

    if request.command != "show":
        raise ServerRequestError(f"Unsupported command: {request.command}")

    response, _events = _collect_stage_command_response(
        config,
        request,
        event_source=event_source,
    )
    return response


def execute_stage_autocomplete_response(
    config: ServerConfig,
    request: StageAutocompleteRequest,
) -> dict[str, object]:
    """Resolve stage label completions for the current workspace selection."""

    workspace_root_request = ServerCommandRequest(
        request_id="stage-autocomplete",
        command="show",
        arguments=("@autocomplete//request",),
        options=(
            {"workspaceRoot": request.workspace_root}
            if request.workspace_root is not None
            else {}
        ),
    )
    workspace_root = _workspace_root_from_request(config, workspace_root_request)
    workspace = _baseline_workspace_for_root(config, workspace_root)
    return stage_autocomplete_payload(workspace, request.breadcrumb, request.prompt)


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
        self._stage_request_logs: OrderedDict[str, list[CommandEvent]] = OrderedDict()
        self._stage_request_logs_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._restart_requested = False
        super().__init__(server_address, MlodyApiRequestHandler)

    def store_stage_request_logs(
        self,
        request_id: str,
        events: Sequence[Mapping[str, object]],
    ) -> None:
        sanitized_events = [
            {
                key: value
                for key, value in event.items()
                if key not in {"stageResult", "value", "sequence"}
            }
            for event in events
        ]
        with self._stage_request_logs_lock:
            self._stage_request_logs[request_id] = sanitized_events
            self._stage_request_logs.move_to_end(request_id)
            while len(self._stage_request_logs) > _MAX_STAGE_REQUEST_LOGS:
                self._stage_request_logs.popitem(last=False)

    def get_stage_request_logs(self, request_id: str) -> list[CommandEvent] | None:
        with self._stage_request_logs_lock:
            events = self._stage_request_logs.get(request_id)
            if events is None:
                return None
            return [dict(event) for event in events]

    def request_restart(self) -> str:
        with self._restart_lock:
            if self._restart_requested:
                return self.server_config.instance_id
            self._restart_requested = True

        try:
            _spawn_restart_watcher(self.server_config)
        except Exception:
            with self._restart_lock:
                self._restart_requested = False
            raise
        threading.Thread(
            target=self._shutdown_after_restart_request,
            name="mlody-server-restart",
            daemon=True,
        ).start()
        return self.server_config.instance_id

    def _shutdown_after_restart_request(self) -> None:
        time.sleep(0.15)
        self.shutdown()


def _server_workspace_payload(config: ServerConfig) -> dict[str, object]:
    return {
        "workspaceRoot": _workspace_root_to_relative(
            config.workspace_root,
            config.monorepo_root,
        ),
        "roots": str(config.roots) if config.roots is not None else None,
        "fullWorkspace": config.full_workspace,
    }


def _server_health_payload(server: MlodyApiServer) -> dict[str, object]:
    return {
        "status": "ok",
        "instanceId": server.server_config.instance_id,
        "http": {
            "host": server.server_address[0],
            "port": server.server_port,
        },
        "lsp": {
            "host": server.server_config.lsp_host,
            "port": server.server_config.lsp_port,
            "transport": "tcp",
        },
        "workspace": _server_workspace_payload(server.server_config),
    }


def _server_status_payload(server: MlodyApiServer) -> dict[str, object]:
    with server._stage_request_logs_lock:
        retained_stage_request_count = len(server._stage_request_logs)
    with server._restart_lock:
        restart_pending = server._restart_requested

    config = server.server_config
    workspace_payload = dict(_server_workspace_payload(config))
    workspace_payload["monorepoRoot"] = str(config.monorepo_root)

    return {
        **_server_health_payload(server),
        "pid": os.getpid(),
        "startedAt": config.started_at.isoformat().replace("+00:00", "Z"),
        "uptimeSeconds": round(
            max(0.0, time.monotonic() - config.started_monotonic),
            3,
        ),
        "currentCwd": os.getcwd(),
        "launchCwd": str(config.restart_cwd),
        "launchArgv": list(config.restart_argv),
        "pythonExecutable": sys.executable,
        "pythonVersion": sys.version.split()[0],
        "platform": sys.platform,
        "threadCount": threading.active_count(),
        "workspace": workspace_payload,
        "logging": {
            "verbose": config.verbose,
            "retainedStageRequestCount": retained_stage_request_count,
            "retainedStageRequestCapacity": _MAX_STAGE_REQUEST_LOGS,
        },
        "restartPending": restart_pending,
    }


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
            self._write_json_response(HTTPStatus.OK, _server_health_payload(self.server))
            return

        if path == "/api/server/status":
            self._write_json_response(HTTPStatus.OK, _server_status_payload(self.server))
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

        if path == "/api/workspaces":
            try:
                payloads: list[dict[str, object]] = []
                for workspace_root in _available_workspace_roots(
                    self.server.server_config
                ):
                    try:
                        workspace = _baseline_workspace_for_root(
                            self.server.server_config,
                            workspace_root,
                        )
                        payloads.append(
                            _workspace_summary_payload(
                                self.server.server_config,
                                workspace,
                                workspace_root=workspace_root,
                            )
                        )
                    except Exception:  # noqa: BLE001
                        _logger.exception(
                            "Failed to load workspace summary for %s",
                            workspace_root,
                        )
                        payloads.append(
                            _default_workspace_summary_payload(
                                self.server.server_config,
                                workspace_root=workspace_root,
                            )
                        )
                self._write_json_response(HTTPStatus.OK, payloads)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load workspaces API payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path == "/api/history":
            try:
                self._write_json_response(
                    HTTPStatus.OK,
                    _load_history_entries(_history_file_path()),
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load command history API payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path.startswith("/api/execute/stage/logs/"):
            request_id = unquote(path.removeprefix("/api/execute/stage/logs/")).strip()
            if request_id == "":
                self._write_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Stage log request id must not be empty."},
                )
                return

            events = self.server.get_stage_request_logs(request_id)
            if events is None:
                self._write_json_response(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"No stage logs found for request id '{request_id}'."},
                )
                return

            self._write_json_response(
                HTTPStatus.OK,
                {
                    "requestId": request_id,
                    "events": events,
                },
            )
            return

        if path == "/" or not path.startswith("/api"):
            static_asset = _resolve_stage_static_asset(path)
            if static_asset is not None:
                self._write_static_response(static_asset)
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
                _record_history_entry(self.server.server_config, request)
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
                _record_history_entry(self.server.server_config, request)
                response, events = _collect_stage_command_response(
                    self.server.server_config,
                    request,
                    event_source=self.server.event_source,
                )
                self.server.store_stage_request_logs(request.request_id, events)
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

        if path == "/api/autocomplete/stage":
            try:
                request = parse_stage_autocomplete_request(payload)
                response = execute_stage_autocomplete_response(
                    self.server.server_config,
                    request,
                )
                self._write_json_response(HTTPStatus.OK, response)
            except ServerRequestError as exc:
                self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except ValueError as exc:
                self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load stage autocomplete payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path == "/api/query/stage/list":
            try:
                request = parse_stage_query_list_request(payload)
                response = execute_stage_query_list_response(
                    self.server.server_config,
                    request,
                )
                self._write_json_response(HTTPStatus.OK, response)
            except ServerRequestError as exc:
                self._write_json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to load stage query list payload")
                self._write_json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
            return

        if path == "/api/server/restart":
            try:
                previous_instance_id = self.server.request_restart()
                self._write_json_response(
                    HTTPStatus.ACCEPTED,
                    {
                        "status": "restarting",
                        "previousInstanceId": previous_instance_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to schedule mlody server restart")
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
            _record_history_entry(self.server.server_config, request)
            response = collect_command_response(
                self.server.server_config,
                request,
                event_source=self.server.event_source,
            )
            self._write_json_response(HTTPStatus.OK, response)
            return

        if path == "/api/execute/stream":
            _record_history_entry(self.server.server_config, request)
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

    def _write_static_response(self, asset_path: Path) -> None:
        try:
            body = asset_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_common_headers()
            self.send_header("Content-Type", _static_content_type(asset_path))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except FileNotFoundError:
            self._write_json_response(
                HTTPStatus.NOT_FOUND,
                {"error": f"Static asset not found: {asset_path.name}"},
            )
        except _CLIENT_DISCONNECT_ERRORS:
            _logger.debug("Client disconnected before static response completed.")


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


def _listener_bind_exception(
    listener_name: str,
    host: str,
    port: int,
    port_flag: str,
    exc: OSError,
) -> click.ClickException:
    endpoint = f"{host}:{port}"
    if exc.errno == errno.EADDRINUSE:
        return click.ClickException(
            f"Could not start {listener_name} on {endpoint}: address already in use. "
            f"Stop the existing process or choose a different {port_flag}."
        )
    if exc.errno == errno.EACCES:
        return click.ClickException(
            f"Could not start {listener_name} on {endpoint}: permission denied."
        )
    return click.ClickException(
        f"Could not start {listener_name} on {endpoint}: {exc}."
    )


def _assert_listener_available(
    listener_name: str,
    host: str,
    port: int,
    *,
    port_flag: str,
) -> None:
    try:
        candidates = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError as exc:
        raise _listener_bind_exception(
            listener_name,
            host,
            port,
            port_flag,
            exc,
        ) from None

    bind_errors: list[OSError] = []
    seen: set[tuple[object, ...]] = set()
    for family, socktype, proto, _canonname, sockaddr in candidates:
        cache_key = (family, socktype, proto, sockaddr)
        if cache_key in seen:
            continue
        seen.add(cache_key)
        try:
            with socket.socket(family, socktype, proto) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(sockaddr)
            return
        except OSError as exc:
            bind_errors.append(exc)

    if bind_errors:
        raise _listener_bind_exception(
            listener_name,
            host,
            port,
            port_flag,
            bind_errors[0],
        ) from None


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


def _display_http_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def run_server(
    config: ServerConfig,
    *,
    event_source: CommandEventSource = iter_command_events,
    lsp_runner: Callable[[ServerConfig], None] = _run_lsp_tcp_server,
) -> None:
    """Run the persistent HTTP+LSP server until interrupted."""

    _assert_listener_available(
        "HTTP API listener",
        config.http_host,
        config.http_port,
        port_flag="--server-port",
    )
    _assert_listener_available(
        "LSP listener",
        config.lsp_host,
        config.lsp_port,
        port_flag="--lsp-port",
    )

    try:
        http_server = create_http_server(config, event_source=event_source)
    except OSError as exc:
        raise _listener_bind_exception(
            "HTTP API listener",
            config.http_host,
            config.http_port,
            "--server-port",
            exc,
        ) from None
    _start_lsp_thread(config, lsp_runner=lsp_runner)

    click.echo(
        f"HTTP API listening on http://{config.http_host}:{http_server.server_port}"
    )
    click.echo(
        f"Stage UI available at http://{_display_http_host(config.http_host)}:{http_server.server_port}/"
    )
    click.echo(f"LSP listening on tcp://{config.lsp_host}:{config.lsp_port}")

    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("Shutting down mlody server")
    finally:
        http_server.shutdown()
        http_server.server_close()
