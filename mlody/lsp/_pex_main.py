"""PEX entry point for the mlody LSP server.

Named _pex_main.py rather than __main__.py to avoid a collision with PEX's
own __main__.py bootstrap when packaging the binary.
"""

from mlody.lsp.server import server

server.start_io()
