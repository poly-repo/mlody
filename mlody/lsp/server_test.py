"""Tests for mlody.lsp.server — workspace hot-reload on file change and
document lifecycle handlers (didOpen, didChange, didClose), hover, and
semantic token handlers.

Covers requirements:
- Full workspace reload when .mlody files change on disk
- Publish diagnostics on document open / change
- Evict cached parse tree on document close
- Surface evaluator runtime errors as diagnostics
- Hover content format and priority ordering (lsp-hover/spec.md)
- Semantic token delta encoding and ERROR-node exclusion (lsp-semantic-tokens/spec.md)
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from lsprotocol import types
from pygls.uris import to_fs_path

import mlody.lsp.server as server_module
from mlody.lsp.server import (
    TOKEN_MODIFIERS,
    TOKEN_TYPES,
    _noop_print,
    on_changed_watched_files,
    on_did_change,
    on_did_close,
    on_did_open,
    on_hover,
    on_semantic_tokens_full,
)


def _make_params(*uris: str) -> types.DidChangeWatchedFilesParams:
    """Build a DidChangeWatchedFilesParams with one Changed event per URI."""
    return types.DidChangeWatchedFilesParams(
        changes=[
            types.FileEvent(uri=uri, type=types.FileChangeType.Changed)
            for uri in uris
        ]
    )


class TestOnChangedWatchedFiles:
    """Requirement: Workspace hot-reload on workspace/didChangeWatchedFiles."""

    def test_on_changed_watched_files_reloads_workspace(
        self, tmp_path: Path
    ) -> None:
        # A fresh Workspace is constructed with the current monorepo root,
        # loaded, and its evaluator replaces the module-level one.
        # The no-op print_fn must be passed so that sandbox print() calls in
        # user scripts do not corrupt the stdout JSON-RPC transport.
        mock_evaluator = MagicMock()
        mock_workspace = MagicMock()
        mock_workspace.evaluator = mock_evaluator

        with (
            patch.object(server_module, "_monorepo_root", tmp_path),
            patch.object(server_module, "_evaluator", None),
            patch(
                "mlody.lsp.server.Workspace", return_value=mock_workspace
            ) as MockWorkspace,
        ):
            on_changed_watched_files(_make_params("file:///mlody/foo.mlody"))

            MockWorkspace.assert_called_once_with(
                monorepo_root=tmp_path, print_fn=_noop_print
            )
            mock_workspace.load.assert_called_once()
            assert server_module._evaluator is mock_evaluator

    def test_on_changed_watched_files_sets_evaluator_none_on_load_failure(
        self, tmp_path: Path
    ) -> None:
        # When Workspace.load() raises, _evaluator is set to None so that
        # completion and definition handlers degrade gracefully rather than
        # serving stale or inconsistent results.
        mock_workspace = MagicMock()
        mock_workspace.load.side_effect = RuntimeError("disk error")

        with (
            patch.object(server_module, "_monorepo_root", tmp_path),
            patch("mlody.lsp.server.Workspace", return_value=mock_workspace),
        ):
            on_changed_watched_files(_make_params("file:///mlody/foo.mlody"))

        assert server_module._evaluator is None

    def test_on_changed_watched_files_logs_event_count(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # On a successful reload the server logs an info record that includes
        # the number of file-change events so operators can see what triggered
        # the reload in the LSP log.
        mock_workspace = MagicMock()
        mock_workspace.evaluator = MagicMock()

        with (
            caplog.at_level(logging.INFO, logger="mlody.lsp.server"),
            patch.object(server_module, "_monorepo_root", tmp_path),
            patch("mlody.lsp.server.Workspace", return_value=mock_workspace),
        ):
            on_changed_watched_files(
                _make_params(
                    "file:///mlody/foo.mlody",
                    "file:///mlody/bar.mlody",
                )
            )

        assert "reloaded" in caplog.text
        assert "2" in caplog.text


class TestDocumentLifecycle:
    """Requirements: Publish diagnostics on open/change; evict cache on close."""

    _VALID_URI = "file:///test_lifecycle_valid.mlody"
    _INVALID_URI = "file:///test_lifecycle_invalid.mlody"
    _CHANGE_URI = "file:///test_lifecycle_change.mlody"
    _CLOSE_URI = "file:///test_lifecycle_close.mlody"
    _EVAL_URI = "file:///test_lifecycle_eval.mlody"

    def _open_params(self, uri: str, text: str, version: int = 1) -> types.DidOpenTextDocumentParams:
        return types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=uri,
                language_id="starlark",
                version=version,
                text=text,
            )
        )

    def _change_params(
        self,
        uri: str,
        text: str,
        version: int = 2,
    ) -> types.DidChangeTextDocumentParams:
        return types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(
                uri=uri,
                version=version,
            ),
            content_changes=[
                types.TextDocumentContentChangeWholeDocument(text=text),
            ],
        )

    def _close_params(self, uri: str) -> types.DidCloseTextDocumentParams:
        return types.DidCloseTextDocumentParams(
            text_document=types.TextDocumentIdentifier(uri=uri)
        )

    def test_did_open_valid_document_publishes_empty_diagnostics(self) -> None:
        """Scenario: Valid document on open clears diagnostics (empty list)."""
        with patch.object(
            server_module.server,
            "text_document_publish_diagnostics",
        ) as mock_publish:
            on_did_open(self._open_params(self._VALID_URI, "x = 1\n"))

        mock_publish.assert_called_once()
        params = mock_publish.call_args[0][0]
        assert params.uri == self._VALID_URI
        assert params.diagnostics == []

    def test_did_open_syntax_error_publishes_error_diagnostic(self) -> None:
        """Scenario: Syntax error on open produces a diagnostic."""
        with patch.object(
            server_module.server,
            "text_document_publish_diagnostics",
        ) as mock_publish:
            on_did_open(self._open_params(self._INVALID_URI, "def ((("))

        mock_publish.assert_called_once()
        params = mock_publish.call_args[0][0]
        assert params.uri == self._INVALID_URI
        assert len(params.diagnostics) >= 1

    def test_did_change_syntax_error_publishes_diagnostic(self) -> None:
        """Scenario: Introducing a syntax error shows a new diagnostic."""
        with patch.object(
            server_module.server,
            "text_document_publish_diagnostics",
        ) as mock_publish:
            on_did_change(self._change_params(self._CHANGE_URI, "def ((("))

        mock_publish.assert_called_once()
        params = mock_publish.call_args[0][0]
        assert params.uri == self._CHANGE_URI
        assert len(params.diagnostics) >= 1

    def test_did_close_removes_uri_from_cache(self) -> None:
        """Scenario: Closed document is evicted from cache."""
        # Pre-populate the cache so there is something to remove.
        server_module.CACHE.update(self._CLOSE_URI, version=1, text="x = 1\n")

        with patch.object(server_module.CACHE, "remove") as mock_remove:
            on_did_close(self._close_params(self._CLOSE_URI))

        mock_remove.assert_called_once_with(self._CLOSE_URI)

    def test_did_open_with_eval_error_includes_eval_diagnostic(self) -> None:
        """Scenario: NameError from evaluator appears as a diagnostic when doc is opened."""
        eval_exc = NameError("name 'missing_pipeline' is not defined")

        with (
            patch.object(server_module, "_eval_error", eval_exc),
            patch.object(
                server_module.server,
                "text_document_publish_diagnostics",
            ) as mock_publish,
        ):
            on_did_open(self._open_params(self._EVAL_URI, "x = 1\n"))

        mock_publish.assert_called_once()
        params = mock_publish.call_args[0][0]
        # Valid doc has no parse errors; eval error should be the only diagnostic.
        assert len(params.diagnostics) >= 1
        messages = [d.message for d in params.diagnostics]
        assert any(m.startswith("NameError:") for m in messages), (
            f"Expected a NameError diagnostic; got: {messages}"
        )


class TestNoopPrint:
    """Requirement: _noop_print never writes to stdout.

    The LSP server passes _noop_print as the sandbox print_fn so that
    print() calls in user .mlody scripts are silently discarded rather
    than corrupting the JSON-RPC stdio transport.
    """

    def test_noop_print_produces_no_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _noop_print("anything", "at all", sep=", ", end="\n")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_noop_print_accepts_arbitrary_args(self) -> None:
        # Must not raise regardless of what a .mlody script passes.
        _noop_print()
        _noop_print("one")
        _noop_print("one", "two", sep=" | ")
        _noop_print(42, True, None, end="")


# ---------------------------------------------------------------------------
# TestHover — lsp-hover/spec.md scenarios
# ---------------------------------------------------------------------------


class TestHover:
    """Requirements: Hover content format and priority ordering (lsp-hover/spec.md)."""

    def _hover(
        self,
        source: str,
        uri: str,
        line: int,
        char: int,
    ) -> types.Hover | None:
        """Call on_hover with a mocked workspace document.

        Patches server_module.server so the handler's workspace call resolves
        to a mock document carrying the given source text.
        """
        mock_doc = MagicMock()
        mock_doc.source = source
        mock_doc.version = 1

        params = types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=line, character=char),
        )
        with patch.object(server_module, "server") as mock_srv:
            mock_srv.workspace.get_text_document.return_value = mock_doc
            return on_hover(params)

    def test_hover_load_path_resolved(self, tmp_path: Path) -> None:
        """Scenario: Load path hover content format — resolved path shown."""
        # _resolve_load_path is imported into server.py; patch that reference.
        resolved = tmp_path / "mlody" / "foo.mlody"
        source = 'load("//mlody/foo.mlody", "X")\n'

        with patch.object(server_module, "_resolve_load_path", return_value=resolved):
            result = self._hover(source, "file:///test_hover_lp_resolved.mlody", 0, 8)

        assert result is not None
        content = result.contents.value  # type: ignore[union-attr]
        assert str(resolved) in content

    def test_hover_load_path_not_found(self) -> None:
        """Scenario: Hover shows (file not found) when load path does not resolve."""
        source = 'load("//mlody/missing.mlody", "X")\n'

        with patch.object(server_module, "_resolve_load_path", return_value=None):
            result = self._hover(source, "file:///test_hover_lp_missing.mlody", 0, 8)

        assert result is not None
        content = result.contents.value  # type: ignore[union-attr]
        assert "(file not found)" in content

    def test_hover_eval_value(self) -> None:
        """Scenario: Evaluated symbol hover content format — Value: `repr` shown."""
        source = "MY_CONST\n"
        uri = "file:///test_hover_eval.mlody"
        # current_file is derived from the URI inside on_hover via to_fs_path.
        current_file = Path(to_fs_path(uri) or uri)  # type: ignore[arg-type]

        mock_evaluator = MagicMock()
        mock_evaluator._module_globals = {current_file: {"MY_CONST": "hello"}}

        with patch.object(server_module, "_evaluator", mock_evaluator):
            result = self._hover(source, uri, 0, 0)

        assert result is not None
        content = result.contents.value  # type: ignore[union-attr]
        assert "Value: `'hello'`" in content

    def test_hover_node_type_fallback(self) -> None:
        """Scenario: Node-type fallback hover content format — **{type}** shown."""
        # `def` keyword at (0,0): not a string node, not an identifier → priority-3 fires.
        source = "def foo(): pass\n"
        result = self._hover(source, "file:///test_hover_fallback.mlody", 0, 0)

        assert result is not None
        content = result.contents.value  # type: ignore[union-attr]
        assert "**def**" in content

    def test_hover_evaluator_none_load_path_still_works(self, tmp_path: Path) -> None:
        """Scenario: Load path priority is independent of evaluator state."""
        # Even when _evaluator is None (workspace load failed), hovering over a
        # load() path string must still resolve and show the path.
        resolved = tmp_path / "mlody" / "foo.mlody"
        source = 'load("//mlody/foo.mlody", "X")\n'

        with (
            patch.object(server_module, "_evaluator", None),
            patch.object(server_module, "_resolve_load_path", return_value=resolved),
        ):
            result = self._hover(source, "file:///test_hover_lp_noneval.mlody", 0, 8)

        assert result is not None
        content = result.contents.value  # type: ignore[union-attr]
        assert str(resolved) in content

    def test_hover_returns_none_for_empty_node_type(self) -> None:
        """Scenario: Priority-3 fallback returns None for nodes with empty type string."""
        # Craft a mock node with type == "" to exercise the None-return branch
        # in the priority-3 fallback without depending on a specific tree shape.
        mock_node = MagicMock()
        mock_node.type = ""

        with patch.object(server_module, "node_at_position", return_value=mock_node):
            result = self._hover("", "file:///test_hover_empty_type.mlody", 0, 0)

        assert result is None


# ---------------------------------------------------------------------------
# TestSemanticTokens — lsp-semantic-tokens/spec.md scenarios
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestIncrementalSync — lsp-incremental-sync/spec.md scenarios
# ---------------------------------------------------------------------------


class TestIncrementalSync:
    """Requirements: Incremental capability negotiation and on_did_change buffer
    reconstruction (lsp-incremental-sync/spec.md).
    """

    _SYNC_URI = "file:///test_incremental_sync.mlody"
    _MISSING_URI = "file:///test_incremental_missing.mlody"

    def _incremental_change_params(
        self,
        uri: str,
        changes: list[
            types.TextDocumentContentChangePartial
            | types.TextDocumentContentChangeWholeDocument
        ],
        version: int = 2,
    ) -> types.DidChangeTextDocumentParams:
        return types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(
                uri=uri,
                version=version,
            ),
            content_changes=changes,
        )

    def test_server_advertises_incremental_sync_kind(self) -> None:
        """Scenario: Server capability negotiation — textDocumentSync.change == 2.

        TextDocumentSyncKind.Incremental == 2 per LSP spec.
        """
        # Verify the module-level server was constructed with Incremental sync kind.
        assert server_module.server._text_document_sync_kind == types.TextDocumentSyncKind.Incremental
        assert types.TextDocumentSyncKind.Incremental == 2  # noqa: PLR2004

    def test_on_did_change_partial_reconstructs_buffer(self) -> None:
        """Scenario: Partial edit updates cached text.

        Seeds CACHE with "A = 1\\n", applies a partial change inserting
        "B = 2\\n" at line 1 character 0, and verifies the cache holds the
        reconstructed full text.
        """
        uri = self._SYNC_URI
        # Seed cache via on_did_open so CACHE holds the initial text.
        with patch.object(server_module.server, "text_document_publish_diagnostics"):
            on_did_open(
                types.DidOpenTextDocumentParams(
                    text_document=types.TextDocumentItem(
                        uri=uri,
                        language_id="starlark",
                        version=1,
                        text="A = 1\n",
                    )
                )
            )

        assert server_module.CACHE.get_text(uri) == "A = 1\n"

        # Partial change: insert "B = 2\n" at end (line 1, char 0).
        with patch.object(server_module.server, "text_document_publish_diagnostics"):
            on_did_change(
                self._incremental_change_params(
                    uri=uri,
                    changes=[
                        types.TextDocumentContentChangePartial(
                            range=types.Range(
                                start=types.Position(line=1, character=0),
                                end=types.Position(line=1, character=0),
                            ),
                            text="B = 2\n",
                        )
                    ],
                    version=2,
                )
            )

        assert server_module.CACHE.get_text(uri) == "A = 1\nB = 2\n"

    def test_on_did_change_missing_cache_uses_empty_baseline(self) -> None:
        """Scenario: Missing cache entry treated as empty document.

        Calls on_did_change for a URI with no cached text and confirms
        no exception is raised (baseline "" is used transparently).
        """
        uri = self._MISSING_URI
        # Ensure the URI is not in cache.
        server_module.CACHE.remove(uri)
        assert server_module.CACHE.get_text(uri) is None

        with patch.object(server_module.server, "text_document_publish_diagnostics"):
            # Must not raise even though there is no cached baseline.
            on_did_change(
                self._incremental_change_params(
                    uri=uri,
                    changes=[
                        types.TextDocumentContentChangeWholeDocument(text="X = 1\n")
                    ],
                )
            )

        assert server_module.CACHE.get_text(uri) == "X = 1\n"


# ---------------------------------------------------------------------------
# TestSemanticTokens — lsp-semantic-tokens/spec.md scenarios
# ---------------------------------------------------------------------------


class TestSemanticTokens:
    """Requirements: Semantic token encoding and ERROR-node exclusion
    (lsp-semantic-tokens/spec.md).
    """

    def _semantic_tokens(self, source: str, uri: str) -> list[int]:
        """Call on_semantic_tokens_full and return the flat data array."""
        mock_doc = MagicMock()
        mock_doc.source = source
        mock_doc.version = 1

        params = types.SemanticTokensParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
        )
        with patch.object(server_module, "server") as mock_srv:
            mock_srv.workspace.get_text_document.return_value = mock_doc
            result = on_semantic_tokens_full(params)
        return list(result.data)

    def test_semantic_tokens_empty_document(self) -> None:
        """Scenario: Empty document produces no tokens — SemanticTokens(data=[])."""
        data = self._semantic_tokens("", "file:///test_semtok_empty.mlody")
        assert data == []

    def test_semantic_tokens_keyword_classified(self) -> None:
        """Scenario: Token type index matches legend position for keywords."""
        # `def` in a function definition must appear with type index == keyword index.
        data = self._semantic_tokens(
            "def foo(): pass\n",
            "file:///test_semtok_keyword.mlody",
        )
        keyword_idx = TOKEN_TYPES.index("keyword")
        token_type_indices = [data[i * 5 + 3] for i in range(len(data) // 5)]
        assert keyword_idx in token_type_indices

    def test_semantic_tokens_variable_with_modifiers(self) -> None:
        """Scenario: Assignment LHS gets variable type + definition|readonly modifiers."""
        # MY_CONST = 42
        # MY_CONST → variable (index 3), modifiers definition(bit0)|readonly(bit1) = 3
        # 42       → number (index 2), modifiers = 0
        data = self._semantic_tokens("MY_CONST = 42\n", "file:///test_semtok_var.mlody")

        variable_idx = TOKEN_TYPES.index("variable")
        number_idx = TOKEN_TYPES.index("number")

        token_tuples = [
            (data[i * 5], data[i * 5 + 1], data[i * 5 + 2], data[i * 5 + 3], data[i * 5 + 4])
            for i in range(len(data) // 5)
        ]

        # MY_CONST: variable with definition|readonly (bitfield = 3)
        assert any(t[3] == variable_idx and t[4] == 3 for t in token_tuples), (  # noqa: PLR2004
            f"Expected variable token with mods=3; got tuples: {token_tuples}"
        )
        # 42: number with no modifiers
        assert any(t[3] == number_idx and t[4] == 0 for t in token_tuples), (
            f"Expected number token with mods=0; got tuples: {token_tuples}"
        )

    def test_semantic_tokens_delta_encoding_same_line(self) -> None:
        """Scenario: Two tokens on same line → second token deltaLine==0,
        deltaStartChar == column difference."""
        # x = 1: x at (0,0), 1 at (0,4) — column difference = 4
        data = self._semantic_tokens("x = 1\n", "file:///test_semtok_sameline.mlody")

        assert len(data) >= 10, f"Expected ≥2 tokens (≥10 ints), got {len(data)}: {data}"  # noqa: PLR2004
        # Second token starts at index 5.
        delta_line_second = data[5]
        delta_col_second = data[6]
        assert delta_line_second == 0
        # x at col 0, 1 at col 4 → deltaStartChar = 4 - 0 = 4
        assert delta_col_second == 4  # noqa: PLR2004

    def test_semantic_tokens_delta_encoding_different_lines(self) -> None:
        """Scenario: Token on a new line → deltaLine equals the line difference."""
        # x = 1\ny = 2: x(0,0), 1(0,4), y(1,0), 2(1,4)
        # Third token (y) is on line 1; line difference from prev (line 0) = 1.
        data = self._semantic_tokens(
            "x = 1\ny = 2\n",
            "file:///test_semtok_difflines.mlody",
        )

        assert len(data) >= 15, f"Expected ≥3 tokens (≥15 ints), got {len(data)}: {data}"  # noqa: PLR2004
        # Third token is at index 10.
        delta_line_third = data[10]
        assert delta_line_third == 1

    def test_semantic_tokens_error_node_skipped(self) -> None:
        """Scenario: Syntax error does not corrupt output — valid SemanticTokens returned."""
        # 'def (((' produces a single ERROR node in tree-sitter-starlark.
        # _collect_tokens must skip it and return a valid (possibly empty) result.
        data = self._semantic_tokens("def (((", "file:///test_semtok_error.mlody")
        # Result must be a valid list of integers (no exception raised).
        assert isinstance(data, list)
        assert len(data) % 5 == 0

    def test_semantic_tokens_function_name_classified(self) -> None:
        """Scenario: Function name takes priority over variable fallback.

        Requirement: Identifier tokens are classified by context — function
        definition name is typed as `function` with modifier `definition`
        (lsp-semantic-tokens/spec.md, priority 1).
        """
        data = self._semantic_tokens(
            "def train(): pass\n",
            "file:///test_semtok_funcname.mlody",
        )
        function_idx = TOKEN_TYPES.index("function")
        definition_mod = 1 << TOKEN_MODIFIERS.index("definition")

        token_tuples = [
            (data[i * 5 + 3], data[i * 5 + 4])
            for i in range(len(data) // 5)
        ]
        assert any(t[0] == function_idx and t[1] == definition_mod for t in token_tuples), (
            f"Expected a token with type=function and modifier=definition; got: {token_tuples}"
        )

    def test_semantic_tokens_parameter_classified(self) -> None:
        """Scenario: Simple parameter is classified as parameter+definition.

        Requirement: Identifier is a direct child of a `parameters` node →
        type `parameter`, modifier `definition`
        (lsp-semantic-tokens/spec.md, priority 2).
        """
        data = self._semantic_tokens(
            "def f(x): pass\n",
            "file:///test_semtok_param.mlody",
        )
        parameter_idx = TOKEN_TYPES.index("parameter")
        definition_mod = 1 << TOKEN_MODIFIERS.index("definition")

        token_tuples = [
            (data[i * 5 + 3], data[i * 5 + 4])
            for i in range(len(data) // 5)
        ]
        assert any(t[0] == parameter_idx and t[1] == definition_mod for t in token_tuples), (
            f"Expected a token with type=parameter and modifier=definition; got: {token_tuples}"
        )

    def test_semantic_tokens_parameter_legend_declared(self) -> None:
        """Scenario: Legend includes `parameter` as a declared token type.

        Requirement: The SemanticTokensLegend SHALL include `parameter` as a
        token type (lsp-semantic-tokens/spec.md — MODIFIED requirement on legend).
        """
        assert "parameter" in TOKEN_TYPES
