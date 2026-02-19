"""Tests for LSPLogHandler — level mapping and message formatting."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from lsprotocol.types import LogMessageParams, MessageType

from mlody.lsp.log_handler import LSPLogHandler


@pytest.fixture
def mock_ls() -> MagicMock:
    return MagicMock()


@pytest.fixture
def handler(mock_ls: MagicMock) -> LSPLogHandler:
    return LSPLogHandler(mock_ls)


def _make_record(level: int, msg: str = "test message") -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


def _emitted_params(mock_ls: MagicMock) -> LogMessageParams:
    """Extract the LogMessageParams passed to window_log_message."""
    mock_ls.window_log_message.assert_called_once()
    (params,), _ = mock_ls.window_log_message.call_args
    return params


def test_emit_error_level_sends_message_type_error(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    # Spec: ERROR records must map to MessageType.Error
    handler.emit(_make_record(logging.ERROR))
    assert _emitted_params(mock_ls).type == MessageType.Error


def test_emit_critical_level_sends_message_type_error(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    # CRITICAL >= ERROR, so it also maps to MessageType.Error
    handler.emit(_make_record(logging.CRITICAL))
    assert _emitted_params(mock_ls).type == MessageType.Error


def test_emit_warning_level_sends_message_type_warning(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    handler.emit(_make_record(logging.WARNING))
    assert _emitted_params(mock_ls).type == MessageType.Warning


def test_emit_info_level_sends_message_type_info(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    handler.emit(_make_record(logging.INFO))
    assert _emitted_params(mock_ls).type == MessageType.Info


def test_emit_debug_level_sends_message_type_log(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    handler.emit(_make_record(logging.DEBUG))
    assert _emitted_params(mock_ls).type == MessageType.Log


def test_emit_passes_formatted_message_to_window_log_message(
    handler: LSPLogHandler, mock_ls: MagicMock
) -> None:
    # The message field of LogMessageParams must be the formatted record text.
    record = _make_record(logging.INFO, msg="workspace loaded")
    handler.emit(record)
    assert _emitted_params(mock_ls).message == handler.format(record)
