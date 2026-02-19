"""Entry point for the mlody LSP server — run with `python -m mlody.lsp`."""

from mlody.lsp.server import server

server.start_io()
