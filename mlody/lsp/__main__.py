"""Entry point for the mlody LSP server — run with `python -m mlody.lsp`."""

import sys

_VERSION = "0.1.0"

# Check for --version before importing the server so the flag exits immediately
# without starting stdio I/O or pulling in the full server dependency graph.
if sys.argv[1:] == ["--version"]:
    print(f"mlody-lsp {_VERSION}")
    sys.exit(0)

from mlody.lsp.server import server

server.start_io()
