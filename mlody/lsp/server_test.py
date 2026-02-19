"""Tests for mlody.lsp.server — workspace hot-reload on file change.

Covers requirement: Full workspace reload when .mlody files change on disk
(workspace/didChangeWatchedFiles handler).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from lsprotocol import types

import mlody.lsp.server as server_module
from mlody.lsp.server import _noop_print, on_changed_watched_files


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
