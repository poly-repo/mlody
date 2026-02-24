"""Tests for mlody.lsp.parser — tree-sitter grammar loading, document cache,
and tree traversal helpers.

Each test class traces to a named requirement in
openspec/changes/lsp-parser-cache/specs/lsp-parser-cache/spec.md.
"""

from __future__ import annotations

import tree_sitter
import pytest

import mlody.lsp.parser as parser
from mlody.lsp.parser import (
    CACHE,
    STARLARK_LANGUAGE,
    DocumentCache,
    ImportedSymbol,
    LoadStatement,
    find_ancestor,
    get_load_statements,
    node_at_position,
    node_contains_position,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(text: str) -> tree_sitter.Tree:
    """Parse *text* and return the tree (uses the module-level parser)."""
    cache = DocumentCache()
    return cache.update("file:///test.mlody", version=1, text=text)


# ---------------------------------------------------------------------------
# 7.1 / 7.2  Grammar node type name verification
# ---------------------------------------------------------------------------

class TestGrammarNodeTypes:
    """Verify the exact node.type strings emitted by tree-sitter-starlark 1.3.0.

    If this test class fails it means the grammar's node type names have
    changed upstream and get_load_statements() must be updated accordingly.
    See design.md §D5.
    """

    def test_load_call_node_type_is_call(self) -> None:
        """Requirement: get_load_statements walks 'call' nodes (spec D5)."""
        tree = _parse('load("//a.mlody", "X")\n')
        root = tree.root_node
        # The top-level statement is an expression_statement wrapping the call.
        stmt = root.children[0]
        call_node = stmt.children[0] if stmt.children else stmt
        assert call_node.type == "call"

    def test_function_identifier_node_type_is_identifier(self) -> None:
        """Requirement: function name inside a call is an 'identifier' node."""
        tree = _parse('load("//a.mlody", "X")\n')
        root = tree.root_node
        stmt = root.children[0]
        call_node = stmt.children[0] if stmt.children else stmt
        func_node = call_node.children[0]
        assert func_node.type == "identifier"
        assert func_node.text == b"load"

    def test_argument_list_node_type(self) -> None:
        """Requirement: argument list has type 'argument_list' (spec D5)."""
        tree = _parse('load("//a.mlody", "X")\n')
        root = tree.root_node
        stmt = root.children[0]
        call_node = stmt.children[0] if stmt.children else stmt
        arg_list = call_node.children[1]
        assert arg_list.type == "argument_list"

    def test_string_argument_node_type(self) -> None:
        """Requirement: string literals inside argument_list are 'string' nodes."""
        tree = _parse('load("//a.mlody", "X")\n')
        root = tree.root_node
        stmt = root.children[0]
        call_node = stmt.children[0] if stmt.children else stmt
        arg_list = call_node.children[1]
        string_nodes = [c for c in arg_list.children if c.type == "string"]
        assert len(string_nodes) == 2  # noqa: PLR2004
        assert string_nodes[0].text == b'"//a.mlody"'
        assert string_nodes[1].text == b'"X"'


# ---------------------------------------------------------------------------
# 8.1  Grammar loading
# ---------------------------------------------------------------------------

class TestGrammarLoading:
    """Requirement: Load the Starlark grammar at import time."""

    def test_starlark_language_is_language_instance(self) -> None:
        """STARLARK_LANGUAGE is a valid tree_sitter.Language after import."""
        assert isinstance(STARLARK_LANGUAGE, tree_sitter.Language)

    def test_parser_module_import_succeeds(self) -> None:
        """Importing mlody.lsp.parser does not raise when grammar is installed."""
        import mlody.lsp.parser  # noqa: F401 — just verifying import is clean


# ---------------------------------------------------------------------------
# 8.2  DocumentCache
# ---------------------------------------------------------------------------

class TestDocumentCache:
    """Requirement: Cache parse trees per document version."""

    def setup_method(self) -> None:
        self.cache = DocumentCache()

    def test_first_update_returns_tree(self) -> None:
        """Scenario: Document is parsed on first update."""
        tree = self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        assert isinstance(tree, tree_sitter.Tree)

    def test_get_after_update_returns_same_tree(self) -> None:
        """Scenario: Document is parsed on first update — get() returns the same tree."""
        tree = self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        assert self.cache.get("file:///a.mlody") is tree

    def test_same_version_returns_same_object(self) -> None:
        """Scenario: Same version is not re-parsed."""
        t1 = self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        t2 = self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        assert t1 is t2

    def test_new_version_re_parses(self) -> None:
        """Scenario: New version triggers a re-parse."""
        t1 = self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        t2 = self.cache.update("file:///a.mlody", version=2, text='y = 2\n')
        assert t1 is not t2

    def test_cache_miss_returns_none(self) -> None:
        """Scenario: Cache miss returns None."""
        assert self.cache.get("file:///never-seen.mlody") is None

    def test_remove_evicts_entry(self) -> None:
        """Scenario: Closed document is evicted."""
        self.cache.update("file:///a.mlody", version=1, text='x = 1\n')
        self.cache.remove("file:///a.mlody")
        assert self.cache.get("file:///a.mlody") is None

    def test_remove_missing_uri_does_not_raise(self) -> None:
        """remove() is idempotent for URIs not in the cache."""
        self.cache.remove("file:///nonexistent.mlody")  # must not raise

    def test_syntax_error_returns_tree_with_has_error(self) -> None:
        """Requirement: Return a tree even for documents with syntax errors."""
        tree = self.cache.update("file:///bad.mlody", version=1, text='def (((')
        assert isinstance(tree, tree_sitter.Tree)
        assert tree.root_node.has_error is True

    def test_multiple_uris_are_isolated(self) -> None:
        """Multiple URIs in the same cache do not interfere."""
        ta = self.cache.update("file:///a.mlody", version=1, text='a = 1\n')
        tb = self.cache.update("file:///b.mlody", version=1, text='b = 2\n')
        assert ta is not tb
        assert self.cache.get("file:///a.mlody") is ta
        assert self.cache.get("file:///b.mlody") is tb


# ---------------------------------------------------------------------------
# 8.3  node_at_position
# ---------------------------------------------------------------------------

class TestNodeAtPosition:
    """Requirement: Provide helpers for querying the parse tree."""

    def test_returns_deepest_containing_node(self) -> None:
        """Scenario: node_at_position returns the deepest containing node."""
        tree = _parse('x = 1\n')
        # Position 0,0 — inside the identifier 'x'
        node = node_at_position(tree, line=0, character=0)
        # Should be 'identifier' or 'x' — the deepest node at column 0
        assert node.type == "identifier"

    def test_handles_position_past_end(self) -> None:
        """Scenario: node_at_position handles positions past end of document."""
        tree = _parse('x = 1\n')
        node = node_at_position(tree, line=999, character=0)
        # tree.root_node creates a fresh Python wrapper on each access, so
        # object identity fails; compare by type and start_point instead.
        assert node.type == tree.root_node.type
        assert node.start_point == tree.root_node.start_point
        assert node.end_point == tree.root_node.end_point

    def test_returned_node_has_no_children_containing_position(self) -> None:
        """Scenario: returned node has no children containing the position."""
        tree = _parse('x = 1\n')
        node = node_at_position(tree, line=0, character=0)
        # None of the node's children should contain (0, 0)
        for child in node.children:
            assert not node_contains_position(child, 0, 0)


# ---------------------------------------------------------------------------
# 8.4  find_ancestor
# ---------------------------------------------------------------------------

class TestFindAncestor:
    """Requirement: Provide helpers for querying the parse tree."""

    def test_finds_matching_ancestor(self) -> None:
        """Scenario: find_ancestor finds a matching parent node."""
        tree = _parse('x = 1\n')
        # Get the leaf node for 'x' (identifier at 0,0)
        leaf = node_at_position(tree, line=0, character=0)
        # Its ancestor is an assignment
        ancestor = find_ancestor(leaf, "assignment")
        assert ancestor is not None
        assert ancestor.type == "assignment"

    def test_returns_none_when_no_match(self) -> None:
        """Scenario: find_ancestor returns None when no match exists."""
        tree = _parse('x = 1\n')
        leaf = node_at_position(tree, line=0, character=0)
        result = find_ancestor(leaf, "nonexistent_type")
        assert result is None

    def test_returns_none_for_root_node(self) -> None:
        """Scenario: find_ancestor returns None for root node (no parent)."""
        tree = _parse('x = 1\n')
        result = find_ancestor(tree.root_node, "assignment")
        # root_node has no parent, so the walk starts with None immediately
        assert result is None


# ---------------------------------------------------------------------------
# 8.5  node_contains_position
# ---------------------------------------------------------------------------

class TestNodeContainsPosition:
    """Requirement: Provide helpers for querying the parse tree."""

    def test_returns_true_for_position_inside(self) -> None:
        """Scenario: node_contains_position returns True for inside."""
        tree = _parse('xyz = 1\n')
        # 'xyz' is at columns 0–3 on line 0
        ident = node_at_position(tree, line=0, character=0)
        assert ident.type == "identifier"
        assert node_contains_position(ident, 0, 0) is True
        assert node_contains_position(ident, 0, 2) is True

    def test_returns_false_for_position_outside(self) -> None:
        """Scenario: node_contains_position returns False for outside."""
        tree = _parse('xyz = 1\n')
        ident = node_at_position(tree, line=0, character=0)
        assert ident.type == "identifier"
        # 'xyz' ends at column 3 (exclusive)
        assert node_contains_position(ident, 0, 3) is False
        assert node_contains_position(ident, 1, 0) is False


# ---------------------------------------------------------------------------
# 8.6  get_load_statements
# ---------------------------------------------------------------------------

class TestGetLoadStatements:
    """Requirement: Extract all load() statements regardless of line count."""

    def test_single_line_load(self) -> None:
        """Scenario: Single-line load() is extracted."""
        tree = _parse('load("//a.mlody", "X")\n')
        stmts = get_load_statements(tree)
        assert len(stmts) == 1
        assert stmts[0].path == "//a.mlody"
        assert len(stmts[0].symbols) == 1
        assert stmts[0].symbols[0].name == "X"

    def test_multi_line_load(self) -> None:
        """Scenario: Multi-line load() is extracted."""
        src = 'load(\n    "//a.mlody",\n    "X",\n    "Y",\n)\n'
        tree = _parse(src)
        stmts = get_load_statements(tree)
        assert len(stmts) == 1
        assert stmts[0].path == "//a.mlody"
        assert [s.name for s in stmts[0].symbols] == ["X", "Y"]

    def test_multiple_loads(self) -> None:
        """Scenario: Multiple load() calls are all extracted."""
        src = 'load("//a.mlody", "X")\nload("//b.mlody", "Y")\n'
        tree = _parse(src)
        stmts = get_load_statements(tree)
        assert len(stmts) == 2  # noqa: PLR2004
        paths = {s.path for s in stmts}
        assert paths == {"//a.mlody", "//b.mlody"}

    def test_no_loads_returns_empty_list(self) -> None:
        """Scenario: Document with no load() calls returns empty list."""
        tree = _parse('x = 1\ny = 2\n')
        stmts = get_load_statements(tree)
        assert stmts == []

    def test_load_with_multiple_symbols(self) -> None:
        """Scenario: load() with multiple symbols preserves declaration order."""
        tree = _parse('load("//a.mlody", "Alpha", "Beta", "Gamma")\n')
        stmts = get_load_statements(tree)
        assert len(stmts) == 1
        assert [s.name for s in stmts[0].symbols] == ["Alpha", "Beta", "Gamma"]

    def test_load_statement_path_node_is_string_node(self) -> None:
        """path_node is the tree-sitter Node for the path string literal."""
        tree = _parse('load("//a.mlody", "X")\n')
        stmts = get_load_statements(tree)
        assert stmts[0].path_node.type == "string"
        assert stmts[0].path_node.text == b'"//a.mlody"'

    def test_imported_symbol_node_is_string_node(self) -> None:
        """symbols[i].node is the tree-sitter Node for the symbol string literal."""
        tree = _parse('load("//a.mlody", "X")\n')
        stmts = get_load_statements(tree)
        sym = stmts[0].symbols[0]
        assert sym.node.type == "string"
        assert sym.node.text == b'"X"'
