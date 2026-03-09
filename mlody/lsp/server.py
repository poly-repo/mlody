"""mlody LSP server — pygls server with completion and go-to-definition for .mlody files."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import tree_sitter
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path
from rich.console import Console

from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.core.workspace import Workspace
from mlody.lsp.completion import get_completions
from mlody.lsp.definition import _resolve_load_path, get_definition
from mlody.lsp.diagnostics import get_eval_diagnostics, get_parse_diagnostics
from mlody.lsp.log_handler import LSPLogHandler
from mlody.lsp.parser import CACHE, apply_incremental_changes, find_ancestor, node_at_position

_logger = logging.getLogger(__name__)

# INCREMENTAL sync: the client sends only changed ranges on each keystroke.
# apply_incremental_changes() reconstructs the full buffer from the diffs;
# DocumentCache stores the rebuilt text and uses old_tree for incremental
# re-parses (see design.md §D1–D5 in lsp-incremental-sync).
server = LanguageServer(
    "mlody-lsp",
    "v0.1",
    text_document_sync_kind=types.TextDocumentSyncKind.Incremental,
)


# Token type and modifier legends for semantic highlighting (LSP §3.3.11).
# Order is significant: indices are used directly in the encoded integer array
# that the client receives.  Clients that advertise only a subset of these
# types/modifiers silently ignore tokens they do not recognise.
TOKEN_TYPES: list[str] = [
    "keyword",    # 0
    "string",     # 1
    "number",     # 2
    "variable",   # 3
    "function",   # 4
    "parameter",  # 5 — replaces unused "operator"; index of all other types unchanged
    "comment",    # 6
]

TOKEN_MODIFIERS: list[str] = [
    "definition",  # 0 — bit 0 (value 1)
    "readonly",    # 1 — bit 1 (value 2)
]

# Pre-computed bitfields for identifier modifiers.
# _DEFINITION: bit 0 only (value 1) — used for function names and parameters,
#   which are being declared but are not necessarily read-only.
# _DEFINITION_READONLY: bits 0 and 1 (value 3) — used for assignment-target
#   identifiers; Starlark top-level bindings are effectively constants once
#   assigned, analogous to `const` in other languages.
_DEFINITION: int = 1 << TOKEN_MODIFIERS.index("definition")
_DEFINITION_READONLY: int = _DEFINITION | (1 << TOKEN_MODIFIERS.index("readonly"))

# Maps tree-sitter-starlark leaf-node type strings → (token_type_index, modifier_bitfield).
# Only leaf nodes whose type appears here are emitted by _collect_tokens.
# Node type strings verified against tree-sitter-starlark 1.3.0.
_SEMANTIC_TOKEN_MAP: dict[str, tuple[int, int]] = {
    # Starlark keywords — anonymous leaf nodes whose .type equals the keyword literal.
    "def":      (TOKEN_TYPES.index("keyword"), 0),
    "if":       (TOKEN_TYPES.index("keyword"), 0),
    "else":     (TOKEN_TYPES.index("keyword"), 0),
    "elif":     (TOKEN_TYPES.index("keyword"), 0),
    "for":      (TOKEN_TYPES.index("keyword"), 0),
    "in":       (TOKEN_TYPES.index("keyword"), 0),
    "return":   (TOKEN_TYPES.index("keyword"), 0),
    "not":      (TOKEN_TYPES.index("keyword"), 0),
    "and":      (TOKEN_TYPES.index("keyword"), 0),
    "or":       (TOKEN_TYPES.index("keyword"), 0),
    "True":     (TOKEN_TYPES.index("keyword"), 0),
    "False":    (TOKEN_TYPES.index("keyword"), 0),
    "None":     (TOKEN_TYPES.index("keyword"), 0),
    "load":     (TOKEN_TYPES.index("keyword"), 0),
    "pass":     (TOKEN_TYPES.index("keyword"), 0),
    "break":    (TOKEN_TYPES.index("keyword"), 0),
    "continue": (TOKEN_TYPES.index("keyword"), 0),
    "lambda":   (TOKEN_TYPES.index("keyword"), 0),
    # Literals
    "string":     (TOKEN_TYPES.index("string"), 0),
    "integer":    (TOKEN_TYPES.index("number"), 0),
    "float":      (TOKEN_TYPES.index("number"), 0),
    # Identifiers — context override for assignment targets applied in _collect_tokens.
    "identifier": (TOKEN_TYPES.index("variable"), 0),
    # Comments
    "comment":    (TOKEN_TYPES.index("comment"), 0),
}


def _collect_tokens(
    tree: tree_sitter.Tree,  # type: ignore[type-arg]
) -> list[tuple[int, int, int, int, int]]:
    """Depth-first tree walk collecting classified leaf-node tokens.

    Skips ERROR subtrees entirely to avoid emitting token ranges with invalid
    bounds for partially-parsed code.  Only emits tokens for leaf nodes whose
    node.type appears in _SEMANTIC_TOKEN_MAP.

    Identifier nodes are reclassified by context using the priority chain
    defined in D3 (design.md): function name → parameter → assignment LHS →
    variable fallback.

    Returns a list of (line, col, length, type_idx, modifier_bitfield) tuples
    in tree order; caller sorts before delta-encoding.
    """
    tokens: list[tuple[int, int, int, int, int]] = []

    def _walk(node: tree_sitter.Node) -> None:  # type: ignore[type-arg]
        if node.type == "ERROR":
            return  # skip ERROR subtree — ranges may be invalid (D4)

        if not node.children:
            # Leaf node — emit if classifiable.
            if node.type in _SEMANTIC_TOKEN_MAP:
                type_idx, mods = _SEMANTIC_TOKEN_MAP[node.type]

                # Context-sensitive classification for identifier nodes.
                # Priority order (D3 in design.md) — first match wins:
                #   1. Function definition name  → function + definition
                #   2. Simple positional param   → parameter + definition
                #   3. Assignment LHS            → variable + definition + readonly
                #   4. Fallback                  → variable (no modifiers, default)
                if node.type == "identifier":
                    parent = node.parent
                    if (
                        parent is not None
                        and parent.type == "function_definition"
                        and len(parent.children) > 1
                        and parent.children[1].start_point == node.start_point
                    ):
                        # Function name is children[1] of function_definition (D1).
                        type_idx = TOKEN_TYPES.index("function")
                        mods = _DEFINITION
                    elif parent is not None and parent.type == "parameters":
                        # Simple positional parameters are direct identifier
                        # children of parameters (D2).  Non-simple variants
                        # (default_parameter, list_splat_pattern, etc.) have a
                        # different parent type and fall through to the fallback.
                        type_idx = TOKEN_TYPES.index("parameter")
                        mods = _DEFINITION
                    elif (
                        parent is not None
                        and parent.type == "assignment"
                        and parent.children
                        and parent.children[0].start_point == node.start_point
                    ):
                        # Assignment LHS gets definition|readonly to indicate a
                        # new binding (position-based check, not identity).
                        mods = _DEFINITION_READONLY

                start_row, start_col = node.start_point
                end_row, end_col = node.end_point
                # Multi-line tokens (triple-quoted strings) would need special
                # handling; omit them here.  Starlark identifiers and keywords
                # are always single-line, so this only skips uncommon strings.
                if start_row == end_row:
                    tokens.append((start_row, start_col, end_col - start_col, type_idx, mods))
        else:
            for child in node.children:
                _walk(child)

    _walk(tree.root_node)
    return tokens


def _noop_print(*_args: object, **_kwargs: object) -> None:
    """Suppress sandbox print() calls in the LSP process.

    The LSP server speaks JSON-RPC over stdio.  Any write to stdout — including
    print() calls inside user .mlody scripts — corrupts the Content-Length
    framing and causes the client to lose sync.  This no-op is passed as
    print_fn to every Workspace instance created by the server so that sandbox
    print() is silently discarded instead of touching stdout.
    """


# Sink for the Workspace registry dump (rich Console output).
# The LSP server must not write anything to stdout beyond JSON-RPC framing.
_null_console = Console(file=io.StringIO())

# Module-level evaluator state — set on INITIALIZED, read by all request handlers.
# None indicates that the workspace failed to load; handlers degrade gracefully.
_evaluator: Evaluator | None = None
_monorepo_root: Path = Path.cwd()  # overwritten on INITIALIZED

# Last exception raised by Workspace.load(); None after a successful load.
# Stored here so didOpen/didChange can surface workspace errors per-document
# without re-running the evaluator on every keystroke (D2 in design.md).
_eval_error: Exception | None = None


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
    global _evaluator, _monorepo_root, _eval_error  # noqa: PLW0603

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
        workspace = Workspace(monorepo_root=monorepo_root, print_fn=_noop_print, console=_null_console)
        workspace.load()
        _evaluator = workspace.evaluator
        _eval_error = None
    except Exception as exc:  # noqa: BLE001
        # Any failure during workspace load leaves _evaluator as None.
        # All request handlers check for None and return empty/None results.
        # _eval_error is surfaced per-document by didOpen/didChange handlers.
        _evaluator = None
        _eval_error = exc
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
    global _evaluator, _eval_error  # noqa: PLW0603

    try:
        workspace = Workspace(monorepo_root=_monorepo_root, print_fn=_noop_print, console=_null_console)
        workspace.load()
        _evaluator = workspace.evaluator
        _eval_error = None
        _logger.info(
            "Workspace reloaded after file change (%d event(s))",
            len(params.changes),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "Workspace reload failed after file change; completions/definitions degraded"
        )
        _evaluator = None
        _eval_error = exc


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[".", ":"]),
)
def on_completion(params: types.CompletionParams) -> types.CompletionList:
    """Provide context-aware completions for .mlody files."""
    uri = params.text_document.uri
    position = params.position

    document = server.workspace.get_text_document(uri)
    tree = CACHE.update(uri, document.version or 0, document.source)

    raw_path = to_fs_path(uri)
    current_file = Path(raw_path) if raw_path else Path(uri)

    items = get_completions(
        _evaluator,
        _monorepo_root,
        current_file,
        tree,
        position.line,
        position.character,
        document.lines,
    )
    return types.CompletionList(is_incomplete=False, items=items)


def _publish_diagnostics_for(uri: str, version: int, text: str) -> None:
    """Update the parse cache and publish merged diagnostics for *uri*.

    Called from both didOpen and didChange so the pipeline is defined once:
    CACHE.update → get_parse_diagnostics → get_eval_diagnostics → publish.

    Parse diagnostics reflect the current in-editor buffer; eval diagnostics
    reflect the last on-disk workspace load (D2 in design.md — the evaluator
    is not re-run on every keystroke).
    """
    tree = CACHE.update(uri, version, text)
    parse_diags = get_parse_diagnostics(tree)
    eval_diags = get_eval_diagnostics(_eval_error, uri) if _eval_error is not None else []
    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            version=version,
            diagnostics=parse_diags + eval_diags,
        )
    )


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def on_did_open(params: types.DidOpenTextDocumentParams) -> None:
    """Parse the opened document and publish diagnostics.

    Populates the document cache and surfaces any parse errors or workspace
    eval errors immediately when the editor opens a .mlody file.
    """
    td = params.text_document
    _publish_diagnostics_for(td.uri, td.version, td.text)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(params: types.DidChangeTextDocumentParams) -> None:
    """Re-parse the changed document and republish diagnostics.

    With TextDocumentSyncKind.Incremental the client sends only the changed
    range(s).  We retrieve the previous full text from DocumentCache, apply
    the diffs, and pass the reconstructed text to _publish_diagnostics_for.

    If the URI has no cached text (e.g. didOpen was never received), treat
    the baseline as "" to avoid an unhandled None in the diff path (D4 in
    design.md, lsp-incremental-sync).
    """
    uri = params.text_document.uri
    version = params.text_document.version
    prev_text = CACHE.get_text(uri) or ""
    new_text = apply_incremental_changes(prev_text, params.content_changes)
    _publish_diagnostics_for(uri, version, new_text)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def on_did_close(params: types.DidCloseTextDocumentParams) -> None:
    """Evict the document's parse tree from the cache on close."""
    CACHE.remove(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def on_definition(
    params: types.DefinitionParams,
) -> types.Location | None:
    """Provide go-to-definition for load() paths and imported symbols."""
    uri = params.text_document.uri
    position = params.position

    document = server.workspace.get_text_document(uri)
    tree = CACHE.update(uri, document.version or 0, document.source)

    raw_path = to_fs_path(uri)
    current_file = Path(raw_path) if raw_path else Path(uri)

    return get_definition(
        _evaluator,
        _monorepo_root,
        current_file,
        tree,
        position.line,
        position.character,
        document.lines,
    )


@server.feature(types.TEXT_DOCUMENT_HOVER)
def on_hover(params: types.HoverParams) -> types.Hover | None:
    """Provide hover documentation for .mlody files.

    Three priority strategies, first match wins (D3 in design.md):
    1. Cursor on the first string argument of a load() call — show the
       resolved filesystem path (independent of evaluator state, D5).
    2. Cursor on an identifier present in the evaluator globals — show
       Value: `repr(value)`.
    3. Any other node — show **{node.type}**; return None for empty type.
    """
    uri = params.text_document.uri
    position = params.position

    document = server.workspace.get_text_document(uri)
    tree = CACHE.update(uri, document.version or 0, document.source)

    raw_path = to_fs_path(uri)
    current_file = Path(raw_path) if raw_path else Path(uri)

    node = node_at_position(tree, position.line, position.character)

    # Fast-path: empty type means the cursor is not on any meaningful token
    # (e.g. past end of document).  Return None before attempting any ancestry
    # traversal — find_ancestor relies on parent being None at the root.
    if not node.type:
        return None

    # Priority 1: cursor on the path string (first arg) of a load() call.
    # The cursor may land on a string_content leaf inside the string node, so
    # ascend to the enclosing string first — mirrors the pattern in completion.py
    # and definition.py.
    string_node = node if node.type == "string" else find_ancestor(node, "string")
    if string_node is not None:
        arg_list = string_node.parent
        if arg_list is not None and arg_list.type == "argument_list":
            call_node = arg_list.parent
            if call_node is not None and call_node.type == "call":
                func = call_node.children[0]
                if func.type == "identifier" and func.text == b"load":
                    string_args = [c for c in arg_list.children if c.type == "string"]
                    # Only the first string arg is the path; others are symbol names.
                    if string_args and string_args[0].start_point == string_node.start_point:
                        raw = string_node.text or b'""'
                        load_str = raw.decode().strip('"').strip("'")
                        resolved = _resolve_load_path(load_str, _monorepo_root, current_file)
                        if resolved:
                            content = f"**load path**\n`{resolved}`"
                        else:
                            content = f"**load path**\n`{load_str}` (file not found)"
                        return types.Hover(
                            contents=types.MarkupContent(
                                kind=types.MarkupKind.Markdown,
                                value=content,
                            )
                        )

    # Priority 2: identifier whose value is known from the evaluator globals.
    if node.type == "identifier" and _evaluator is not None:
        # pyright: ignore — _module_globals is a private evaluator attribute
        module_globals: dict[str, object] = _evaluator._module_globals.get(  # type: ignore[attr-defined]
            current_file, {}
        )
        ident = (node.text or b"").decode()
        if ident in module_globals:
            value = module_globals[ident]
            content = f"**{ident}**\nValue: `{repr(value)}`"
            return types.Hover(
                contents=types.MarkupContent(
                    kind=types.MarkupKind.Markdown,
                    value=content,
                )
            )

    # Priority 3: node-type fallback — useful for exploratory hover in .mlody files.
    # Empty type is already handled by the early-exit above.
    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=f"**{node.type}**",
        )
    )


@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=TOKEN_TYPES,
        token_modifiers=TOKEN_MODIFIERS,
    ),
)
def on_semantic_tokens_full(
    params: types.SemanticTokensParams,
) -> types.SemanticTokens:
    """Provide full semantic token encoding for .mlody files.

    Emits a delta-encoded integer array per the LSP specification.  Each
    token occupies 5 consecutive integers:
      deltaLine, deltaStartChar, length, tokenType, tokenModifiers

    deltaLine and deltaStartChar are relative to the previous token:
    - Same line (deltaLine == 0): deltaStartChar = col - prev_col
    - New line (deltaLine > 0):   deltaStartChar = col (absolute from line start)
    """
    uri = params.text_document.uri
    document = server.workspace.get_text_document(uri)
    tree = CACHE.update(uri, document.version or 0, document.source)

    raw_tokens = _collect_tokens(tree)
    raw_tokens.sort(key=lambda t: (t[0], t[1]))

    data: list[int] = []
    prev_line = 0
    prev_col = 0
    for line, col, length, type_idx, mods in raw_tokens:
        delta_line = line - prev_line
        delta_col = col - prev_col if delta_line == 0 else col
        data.extend([delta_line, delta_col, length, type_idx, mods])
        prev_line = line
        prev_col = col

    return types.SemanticTokens(data=data)
