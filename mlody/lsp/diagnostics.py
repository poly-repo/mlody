"""Pure diagnostic functions for the mlody LSP server.

No server state — testable in isolation.

Design: see openspec/changes/lsp-diagnostics/design.md (D1).
"""

from __future__ import annotations

import tree_sitter
from lsprotocol import types


def get_parse_diagnostics(tree: tree_sitter.Tree) -> list[types.Diagnostic]:
    """Walk the parse tree and return diagnostics for ERROR and MISSING nodes.

    Each ERROR node produces a "syntax error" diagnostic covering the node's
    full span.  Each MISSING node (parser-inserted placeholders for error
    recovery) produces a "missing <type>" diagnostic at its zero-width point.
    The two cases are distinguished by name so tests can assert on the message.

    Args:
        tree: A tree-sitter Tree; may contain error nodes — callers must not
            assume the tree is error-free before calling this function.

    Returns:
        One Diagnostic per ERROR or MISSING node found in the tree, in
        depth-first traversal order.  Empty list for a syntactically valid
        document.
    """
    diagnostics: list[types.Diagnostic] = []

    def _walk(node: tree_sitter.Node) -> None:  # type: ignore[type-arg]
        if node.type == "ERROR":
            start_row, start_col = node.start_point
            end_row, end_col = node.end_point
            diagnostics.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=start_row, character=start_col),
                        end=types.Position(line=end_row, character=end_col),
                    ),
                    severity=types.DiagnosticSeverity.Error,
                    source="mlody-lsp",
                    message="syntax error",
                )
            )
        elif node.is_missing:
            # MISSING nodes are zero-width — start_point == end_point.
            start_row, start_col = node.start_point
            end_row, end_col = node.end_point
            diagnostics.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=start_row, character=start_col),
                        end=types.Position(line=end_row, character=end_col),
                    ),
                    severity=types.DiagnosticSeverity.Error,
                    source="mlody-lsp",
                    # The word "missing" must appear so clients/tests can distinguish
                    # these from generic parse errors.
                    message=f"missing {node.type}",
                )
            )

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return diagnostics


def get_eval_diagnostics(exc: Exception, uri: str) -> list[types.Diagnostic]:
    """Build a one-element diagnostic list from an evaluator exception.

    Positioning strategy (D2 in design.md):
    - ``SyntaxError``: uses ``exc.lineno`` and ``exc.offset`` directly
      (1-indexed → 0-indexed).
    - All other exceptions: walks ``exc.__traceback__`` looking for a frame
      whose ``co_filename`` appears as a suffix of (or within) the document
      URI.  Falls back to line 0, character 0 when no frame matches.

    Args:
        exc: The exception raised by ``Workspace.load()``.
        uri: The LSP document URI (e.g. ``"file:///path/to/file.mlody"``).

    Returns:
        A list containing exactly one Diagnostic whose message is
        ``"<ExcType>: <message>"``.
    """
    line = 0
    character = 0

    if isinstance(exc, SyntaxError) and exc.lineno is not None:
        # SyntaxError carries source position directly; both lineno and offset
        # are 1-indexed in CPython, so convert to 0-indexed LSP positions.
        line = max(0, exc.lineno - 1)
        character = max(0, (exc.offset or 1) - 1)
    else:
        # Walk the traceback chain for a frame whose filename is a suffix of
        # (or appears inside) the document URI.  Suffix matching handles the
        # common case where co_filename is an absolute FS path and the URI is
        # "file:///abs/path/...".  The co_filename-in-uri fallback covers
        # percent-encoded URIs and Bazel runfiles symlink paths.
        tb = exc.__traceback__
        while tb is not None:
            co_filename = tb.tb_frame.f_code.co_filename
            if co_filename and (uri.endswith(co_filename) or co_filename in uri):
                line = max(0, tb.tb_lineno - 1)
                break
            tb = tb.tb_next

    return [
        types.Diagnostic(
            range=types.Range(
                start=types.Position(line=line, character=character),
                end=types.Position(line=line, character=character),
            ),
            severity=types.DiagnosticSeverity.Error,
            source="mlody-lsp",
            message=f"{type(exc).__name__}: {exc}",
        )
    ]
