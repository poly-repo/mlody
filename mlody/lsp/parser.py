"""Tree-sitter parse infrastructure for the mlody LSP server.

Provides a module-level DocumentCache singleton and helper functions used by
all LSP feature handlers (completion, definition, diagnostics, hover, semantic
tokens).  Grammar and Parser are initialised once at import time; the CACHE
singleton is shared across all handlers because pygls dispatches on a single
asyncio event loop thread.

Design decisions: see openspec/changes/lsp-parser-cache/design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter
from lsprotocol import types

# Grammar and parser are module-level singletons — loaded once at import time.
# If tree-sitter-starlark is absent the ImportError surfaces immediately with
# a clear remediation message rather than propagating as a cryptic AttributeError
# inside a handler.
try:
    import tree_sitter_starlark as _ts_starlark
except ImportError as _exc:
    raise ImportError("tree-sitter-starlark is not installed. Run: o-repin") from _exc

STARLARK_LANGUAGE: tree_sitter.Language = tree_sitter.Language(_ts_starlark.language())

_parser: tree_sitter.Parser = tree_sitter.Parser(STARLARK_LANGUAGE)

# Keys injected by the starlarkish evaluator sandbox at eval time.
# Moved here from completion.py to avoid a circular import: completion.py
# imports from parser.py, so any constant needed by both must live here.
_FRAMEWORK_INTERNALS: frozenset[str] = frozenset(
    {"__builtins__", "load", "__MLODY__", "builtins"}
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedSymbol:
    """A single symbol imported by a load() statement.

    Holds the symbol name as a plain string alongside its source node so that
    handlers can map back to the exact source range for hover / definition.

    NOTE (NFR-001): tree_sitter.Node carries no generic type arguments in
    the current stubs, hence the type: ignore suppressions below.
    """

    name: str
    node: tree_sitter.Node  # type: ignore[type-arg]


@dataclass(frozen=True)
class LoadStatement:
    """A fully parsed load() call extracted from a Starlark file.

    ``path`` is the raw string value (e.g. ``"//mlody/core:workspace.mlody"``).
    ``path_node`` points to the string node in the parse tree so callers can
    compute the source range without re-scanning.
    ``symbols`` preserves the declaration order from the source.

    NOTE (NFR-001): tree_sitter.Node carries no generic type arguments in
    the current stubs, hence the type: ignore suppressions below.
    """

    path: str
    path_node: tree_sitter.Node  # type: ignore[type-arg]
    symbols: list[ImportedSymbol]


# ---------------------------------------------------------------------------
# DocumentCache
# ---------------------------------------------------------------------------


class DocumentCache:
    """In-process cache of tree-sitter parse trees keyed by document URI.

    Maps each URI to a ``(version, text, Tree)`` triple.  Re-parsing happens
    only when the version number changes, preventing redundant work when
    multiple handlers fire for the same document version in a single request
    cycle (e.g. textDocument/completion triggered after textDocument/didChange).

    The stored text is required by ``on_did_change`` to reconstruct the full
    buffer from LSP range-diffs, and by ``extract_top_level_symbols`` for
    live-buffer completion — see design.md §Context.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, str, tree_sitter.Tree]] = {}

    def update(
        self,
        uri: str,
        version: int,
        text: str,
    ) -> tree_sitter.Tree:
        """Return the parse tree for *uri*, re-parsing only if *version* is new.

        Passes the previously stored tree as ``old_tree`` when one exists,
        enabling tree-sitter's incremental re-parse optimisation.  The first
        parse of a URI (cold parse) omits ``old_tree``.

        Args:
            uri: The LSP document URI (e.g. ``"file:///path/to/file.mlody"``).
            version: The document version counter provided by the LSP client.
            text: The full document text as a UTF-8 string.

        Returns:
            A ``tree_sitter.Tree``; may contain error nodes when the document
            has syntax errors — callers must not assume the tree is error-free.
        """
        cached = self._cache.get(uri)
        if cached is not None and cached[0] == version:
            return cached[2]

        # tree-sitter Python bindings require bytes, not str (D3 in design.md).
        # UTF-8 is correct for .mlody files: they are ASCII-safe Starlark, so
        # UTF-8 and UTF-16 byte offsets coincide for all practical content.
        prev_tree = cached[2] if cached is not None else None
        if prev_tree is not None:
            # Incremental re-parse: old_tree is a hint — tree-sitter guarantees
            # correctness from the full text regardless of how stale the hint is.
            tree = _parser.parse(text.encode(), old_tree=prev_tree)
        else:
            tree = _parser.parse(text.encode())
        self._cache[uri] = (version, text, tree)
        return tree

    def get(self, uri: str) -> tree_sitter.Tree | None:
        """Return the cached tree for *uri*, or ``None`` if not present."""
        cached = self._cache.get(uri)
        return cached[2] if cached is not None else None

    def get_text(self, uri: str) -> str | None:
        """Return the cached document text for *uri*, or ``None`` if not present."""
        cached = self._cache.get(uri)
        return cached[1] if cached is not None else None

    def remove(self, uri: str) -> None:
        """Evict the cached tree for *uri* (called on textDocument/didClose)."""
        self._cache.pop(uri, None)


# Module-level cache singleton shared by all LSP handlers.
CACHE: DocumentCache = DocumentCache()


# ---------------------------------------------------------------------------
# Incremental change application
# ---------------------------------------------------------------------------


def apply_incremental_changes(
    text: str,
    changes: list[
        types.TextDocumentContentChangePartial
        | types.TextDocumentContentChangeWholeDocument
    ],
) -> str:
    """Apply an ordered list of LSP range-edits to *text*, returning the updated text.

    Processes each change in the order it appears in *changes* — ranges in
    later changes refer to the text *after* all previous changes have been
    applied, per LSP specification (FR-002).

    Two change variants are handled (D3 in design.md):
    - ``TextDocumentContentChangeWholeDocument``: replaces the entire buffer.
    - ``TextDocumentContentChangePartial``: applies a range splice using the
      line-split algorithm from D2 in design.md.  Splitting on ``"\\n"`` is
      correct for ``.mlody`` files, which are always LF-only.

    Args:
        text: The current full document text.
        changes: Ordered list of LSP content-change events.

    Returns:
        The updated full document text after all changes are applied.
    """
    for change in changes:
        if isinstance(change, types.TextDocumentContentChangeWholeDocument):
            # Whole-document replacement — discard current text entirely.
            text = change.text
        else:
            # Partial (range) change — splice the affected line region.
            start = change.range.start
            end = change.range.end
            lines = text.split("\n")
            prefix = lines[start.line][: start.character]
            suffix = lines[end.line][end.character :]
            replacement_lines = (prefix + change.text + suffix).split("\n")
            lines = lines[: start.line] + replacement_lines + lines[end.line + 1 :]
            text = "\n".join(lines)
    return text


# ---------------------------------------------------------------------------
# Tree traversal helpers
# ---------------------------------------------------------------------------


def node_contains_position(
    node: tree_sitter.Node,  # type: ignore[type-arg]
    line: int,
    character: int,
) -> bool:
    """Return True if (line, character) falls within *node*'s source range.

    Uses (row, column) start/end points from tree-sitter, which are
    zero-based.  The end point is exclusive: a node that ends at column 5
    does NOT contain column 5.
    """
    start_row, start_col = node.start_point
    end_row, end_col = node.end_point

    if line < start_row or line > end_row:
        return False
    if line == start_row and character < start_col:
        return False
    if line == end_row and character >= end_col:
        return False
    return True


def node_at_position(
    tree: tree_sitter.Tree,
    line: int,
    character: int,
) -> tree_sitter.Node:  # type: ignore[type-arg]
    """Return the deepest node in *tree* that contains (line, character).

    Walks children depth-first.  If no node contains the position (e.g. the
    cursor is past the end of the document) returns the root node so callers
    always receive a valid ``Node``.
    """
    root = tree.root_node

    def _descend(node: tree_sitter.Node) -> tree_sitter.Node:  # type: ignore[type-arg]
        for child in node.children:
            if node_contains_position(child, line, character):
                return _descend(child)
        return node

    if not node_contains_position(root, line, character):
        return root
    return _descend(root)


def find_ancestor(
    node: tree_sitter.Node,  # type: ignore[type-arg]
    type_name: str,
) -> tree_sitter.Node | None:  # type: ignore[type-arg]
    """Walk parent links from *node* and return the first ancestor of *type_name*.

    Returns ``None`` if no matching ancestor exists (including when *node* is
    the root and has no parent).
    """
    current = node.parent
    while current is not None:
        if current.type == type_name:
            return current
        current = current.parent
    return None


def extract_top_level_symbols(tree: tree_sitter.Tree) -> list[str]:
    """Return names of syntactically complete top-level bindings in *tree*.

    Walks the direct children of the root ``module`` node (one level deep).
    In tree-sitter-starlark, assignments at module scope are wrapped in an
    ``expression_statement`` parent; ``function_definition`` nodes are direct
    children.  Nodes with errors — ``type == "ERROR"`` or ``has_error`` —
    are skipped entirely, so incomplete assignments (e.g. ``X = struct(``)
    do not appear in the result (tree-sitter collapses them into a top-level
    ERROR node).

    Names starting with ``_`` and names in ``_FRAMEWORK_INTERNALS`` are
    filtered out to avoid surfacing private or evaluator-injected symbols
    as completion candidates.

    See design.md §Decision 3 for the rationale behind the one-level walk
    and the ``has_error`` guard.

    Args:
        tree: A ``tree_sitter.Tree`` for a Starlark document.

    Returns:
        Symbol names in source order, with private and framework names removed.
    """
    symbols: list[str] = []
    for child in tree.root_node.children:
        # Skip syntactically broken nodes.  Incomplete assignments (e.g. an
        # unclosed struct() call) produce a top-level ERROR node rather than
        # an assignment node, so this guard also covers those cases.
        if child.type == "ERROR" or child.has_error:
            continue

        name: str | None = None
        if child.type == "expression_statement" and child.children:
            # In tree-sitter-starlark, assignments at module scope are wrapped
            # in an expression_statement parent node.
            inner = child.children[0]
            if inner.type == "assignment" and inner.children:
                first = inner.children[0]
                if first.type == "identifier" and first.text is not None:
                    name = first.text.decode()
        elif child.type == "function_definition" and len(child.children) > 1:
            # children[1] is the function name identifier; children[0] is "def".
            second = child.children[1]
            if second.type == "identifier" and second.text is not None:
                name = second.text.decode()

        if name is not None and not name.startswith("_") and name not in _FRAMEWORK_INTERNALS:
            symbols.append(name)

    return symbols


def get_load_statements(tree: tree_sitter.Tree) -> list[LoadStatement]:
    """Extract all load() calls from *tree* as structured ``LoadStatement`` objects.

    Handles both single-line and multi-line load() forms because the traversal
    operates on the parse tree rather than raw text.

    Verified grammar node type names (tree-sitter-starlark 1.3.0):
    - load() calls: node.type == "call"
    - function identifier: node.type == "identifier", child of call at index 0
    - argument list: node.type == "argument_list", child of call at index 1
    - path argument (first positional): node.type == "string"
    - symbol arguments: node.type == "string" for positional imports

    The first argument to load() is the path string; all subsequent positional
    string arguments are imported symbol names.  Keyword arguments (aliased
    imports) are excluded from ``symbols`` in this implementation.
    """
    results: list[LoadStatement] = []

    def _walk(node: tree_sitter.Node) -> None:  # type: ignore[type-arg]
        if node.type == "call":
            # The first child of a call node is the function being called.
            if node.child_count >= 2:
                func_node = node.children[0]
                if func_node.type == "identifier" and func_node.text == b"load":
                    arg_list = node.children[1]
                    if arg_list.type == "argument_list":
                        _extract_load(arg_list)

        for child in node.children:
            _walk(child)

    def _extract_load(arg_list: tree_sitter.Node) -> None:  # type: ignore[type-arg]
        # arg_list children include parentheses and commas; filter to string nodes.
        string_args = [c for c in arg_list.children if c.type == "string"]
        if not string_args:
            return

        path_node = string_args[0]
        # tree-sitter returns the raw bytes including surrounding quotes;
        # strip them to get the plain path string.
        raw = path_node.text or b'""'
        path = raw.decode().strip('"').strip("'")

        symbols: list[ImportedSymbol] = []
        for sym_node in string_args[1:]:
            raw_sym = sym_node.text or b'""'
            sym_name = raw_sym.decode().strip('"').strip("'")
            symbols.append(ImportedSymbol(name=sym_name, node=sym_node))

        results.append(LoadStatement(path=path, path_node=path_node, symbols=symbols))

    _walk(tree.root_node)
    return results
