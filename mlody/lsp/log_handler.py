"""LSP log handler — routes Python logging output to the LSP client via window_log_message."""

from __future__ import annotations

import logging

from lsprotocol.types import LogMessageParams, MessageType
from pygls.lsp.server import LanguageServer


class LSPLogHandler(logging.Handler):
    """A logging.Handler that forwards records to the LSP client via window/logMessage."""

    def __init__(self, ls: LanguageServer) -> None:
        super().__init__()
        self.ls = ls

    def emit(self, record: logging.LogRecord) -> None:
        # Default to Log (debug-level) — only elevated for WARNING and above.
        msg_type = MessageType.Log
        if record.levelno >= logging.ERROR:
            msg_type = MessageType.Error
        elif record.levelno >= logging.WARNING:
            msg_type = MessageType.Warning
        elif record.levelno >= logging.INFO:
            msg_type = MessageType.Info

        # pygls 2.x renamed show_message_log() to window_log_message(LogMessageParams)
        self.ls.window_log_message(LogMessageParams(type=msg_type, message=self.format(record)))
