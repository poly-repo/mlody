"""mlody LSP server — pygls server with completion and go-to-definition for .mlody files."""

from __future__ import annotations

import logging
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.core.workspace import Workspace
from mlody.lsp.completion import get_completions
from mlody.lsp.definition import get_definition
from mlody.lsp.log_handler import LSPLogHandler

_logger = logging.getLogger(__name__)

server = LanguageServer("mlody-lsp", "v0.1")


def _noop_print(*_args: object, **_kwargs: object) -> None:
    """Suppress sandbox print() calls in the LSP process.

    The LSP server speaks JSON-RPC over stdio.  Any write to stdout — including
    print() calls inside user .mlody scripts — corrupts the Content-Length
    framing and causes the client to lose sync.  This no-op is passed as
    print_fn to every Workspace instance created by the server so that sandbox
    print() is silently discarded instead of touching stdout.
    """

# Module-level evaluator state — set on INITIALIZED, read by all request handlers.
# None indicates that the workspace failed to load; handlers degrade gracefully.
_evaluator: Evaluator | None = None
_monorepo_root: Path = Path.cwd()  # overwritten on INITIALIZED


@server.feature(types.INITIALIZED)
async def on_initialized(params: types.InitializedParams) -> None:
    """Load the mlody workspace after the LSP handshake completes.

    Uses server.workspace.root_uri (set during the initialize request by pygls)
    rather than the InitializedParams (which carries no root path). Stores the
    evaluator in a module-level variable so request handlers can access it
    without holding a reference to the Workspace object.

    After a successful load, dynamically registers a workspace/didChangeWatchedFiles
    watcher for **/*.mlody so the client notifies us when files change on disk.
    """
    global _evaluator, _monorepo_root  # noqa: PLW0603

    root_uri = server.workspace.root_uri
    if root_uri is None:
        return

    raw = to_fs_path(root_uri)
    if raw is None:
        return

    monorepo_root = Path(raw)
    _monorepo_root = monorepo_root

    # Attach the LSP log handler before loading the workspace so that any
    # log records emitted during load are forwarded to the editor's LSP log.
    # Attached here (not at module import) because show_message_log requires
    # an active LSP connection — attaching earlier would silently drop messages.
    logging.getLogger().addHandler(LSPLogHandler(server))

    try:
        workspace = Workspace(monorepo_root=monorepo_root, print_fn=_noop_print)
        workspace.load()
        _evaluator = workspace.evaluator
    except Exception:  # noqa: BLE001
        # Any failure during workspace load leaves _evaluator as None.
        # All request handlers check for None and return empty/None results.
        _evaluator = None
        return

    _logger.info("Server started")

    # Register a file watcher after a successful load so Eglot notifies the
    # server whenever a .mlody file is created, changed, or deleted.
    # Eglot advertises dynamicRegistration: true for didChangeWatchedFiles,
    # so this request will be honoured without a server restart.
    await server.client_register_capability_async(
        types.RegistrationParams(
            registrations=[
                types.Registration(
                    id="mlody-file-watcher",
                    method=types.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                    register_options=types.DidChangeWatchedFilesRegistrationOptions(
                        watchers=[
                            types.FileSystemWatcher(glob_pattern="**/*.mlody"),
                        ]
                    ),
                )
            ]
        )
    )


@server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def on_changed_watched_files(params: types.DidChangeWatchedFilesParams) -> None:
    """Full workspace reload when any .mlody file changes on disk.

    A full reload is correct because .mlody files reference each other via
    load(), so a single changed file can affect the evaluation of any file
    that transitively loads it.  Incremental invalidation would require a
    dependency graph that does not currently exist.
    """
    global _evaluator  # noqa: PLW0603

    try:
        workspace = Workspace(monorepo_root=_monorepo_root, print_fn=_noop_print)
        workspace.load()
        _evaluator = workspace.evaluator
        _logger.info(
            "Workspace reloaded after file change (%d event(s))",
            len(params.changes),
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Workspace reload failed after file change; completions/definitions degraded"
        )
        _evaluator = None


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[".", ":"]),
)
def on_completion(params: types.CompletionParams) -> types.CompletionList:
    """Provide context-aware completions for .mlody files."""
    uri = params.text_document.uri
    position = params.position

    document = server.workspace.get_text_document(uri)
    lines = document.lines
    line_text = lines[position.line] if position.line < len(lines) else ""
    # Slice to cursor — only the text typed so far on this line.
    line_to_cursor = line_text[: position.character]

    raw_path = to_fs_path(uri)
    current_file = Path(raw_path) if raw_path else Path(uri)

    items = get_completions(_evaluator, _monorepo_root, current_file, line_to_cursor)
    return types.CompletionList(is_incomplete=False, items=items)


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def on_definition(
    params: types.DefinitionParams,
) -> types.Location | None:
    """Provide go-to-definition for load() paths and imported symbols."""
    uri = params.text_document.uri
    position = params.position

    document = server.workspace.get_text_document(uri)
    lines = document.lines
    line_text = lines[position.line] if position.line < len(lines) else ""

    raw_path = to_fs_path(uri)
    current_file = Path(raw_path) if raw_path else Path(uri)

    return get_definition(
        _evaluator,
        _monorepo_root,
        current_file,
        line_text,
        position.character,
    )
