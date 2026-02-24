"""Tests for mlody.lsp.parser — tree-sitter grammar loading, document cache,
and tree traversal helpers.

Each test class traces to a named requirement in
openspec/changes/lsp-parser-cache/specs/lsp-parser-cache/spec.md.
"""

from __future__ import annotations

import tree_sitter
import pytest

import mlody.lsp.parser as parser
from lsprotocol import types

from mlody.lsp.parser import (
    CACHE,
    STARLARK_LANGUAGE,
    DocumentCache,
    ImportedSymbol,
    LoadStatement,
    apply_incremental_changes,
    extract_top_level_symbols,
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


# ---------------------------------------------------------------------------
# Requirement: Store document text alongside the parse tree
# Spec: scenarios in lsp-parser-cache/spec.md § "Store document text"
# ---------------------------------------------------------------------------


class TestDocumentCacheGetText:
    """Requirement: DocumentCache.get_text returns the stored document text."""

    def setup_method(self) -> None:
        self.cache = DocumentCache()
        self.uri = "file:///test.mlody"

    def test_get_text_returns_none_before_first_update(self) -> None:
        """Scenario: get_text returns None before first update."""
        assert self.cache.get_text(self.uri) is None

    def test_get_text_returns_stored_text_after_update(self) -> None:
        """Scenario: Text is accessible after update."""
        self.cache.update(self.uri, version=1, text="abc\n")
        assert self.cache.get_text(self.uri) == "abc\n"

    def test_get_text_reflects_new_version(self) -> None:
        """Scenario: Text updates on new version."""
        self.cache.update(self.uri, version=1, text="first\n")
        self.cache.update(self.uri, version=2, text="second\n")
        assert self.cache.get_text(self.uri) == "second\n"

    def test_get_text_unchanged_on_same_version(self) -> None:
        """Scenario: Same version returns cached text unchanged (no re-parse)."""
        self.cache.update(self.uri, version=1, text="first\n")
        self.cache.update(self.uri, version=1, text="different\n")
        assert self.cache.get_text(self.uri) == "first\n"


# ---------------------------------------------------------------------------
# Requirement: Use the previous parse tree for incremental re-parse
# Spec: scenarios in lsp-parser-cache/spec.md § "Use the previous parse tree"
# ---------------------------------------------------------------------------


class TestDocumentCacheIncrementalReparse:
    """Requirement: DocumentCache passes old_tree on re-parse when available."""

    def setup_method(self) -> None:
        self.cache = DocumentCache()
        self.uri = "file:///test.mlody"

    def test_cold_parse_produces_valid_tree(self) -> None:
        """Scenario: First parse has no previous tree — still returns valid tree."""
        tree = self.cache.update(self.uri, version=1, text="x = 1\n")
        assert isinstance(tree, tree_sitter.Tree)
        assert not tree.root_node.has_error

    def test_reparse_reflects_new_content(self) -> None:
        """Scenario: Re-parse reuses old tree and reflects updated content."""
        self.cache.update(self.uri, version=1, text="x = 1\n")
        tree2 = self.cache.update(self.uri, version=2, text="y = 2\n")
        assert isinstance(tree2, tree_sitter.Tree)
        # Root node text must contain the new identifier, not the old one.
        root_text = tree2.root_node.text
        assert root_text is not None
        assert b"y" in root_text

    def test_reparse_after_remove_is_cold(self) -> None:
        """Scenario: Re-parse after removal starts fresh (no stale tree ref)."""
        self.cache.update(self.uri, version=1, text="x = 1\n")
        self.cache.remove(self.uri)
        # After removal a new update must succeed as a cold parse.
        tree = self.cache.update(self.uri, version=1, text="z = 3\n")
        assert isinstance(tree, tree_sitter.Tree)
        assert not tree.root_node.has_error


# ---------------------------------------------------------------------------
# Requirement: Extract completed top-level symbol names from the parse tree
# Spec: scenarios in lsp-parser-cache/spec.md § "Extract completed top-level"
# ---------------------------------------------------------------------------


class TestExtractTopLevelSymbols:
    """Requirement: extract_top_level_symbols returns names of complete bindings."""

    def test_completed_assignment_is_extracted(self) -> None:
        """Scenario: Completed assignment is extracted."""
        tree = _parse('MY_MODEL = struct(name="bert")\n')
        assert extract_top_level_symbols(tree) == ["MY_MODEL"]

    def test_function_definition_is_extracted(self) -> None:
        """Scenario: Function definition is extracted."""
        tree = _parse("def train():\n    pass\n")
        assert extract_top_level_symbols(tree) == ["train"]

    def test_incomplete_assignment_not_extracted(self) -> None:
        """Scenario: Incomplete assignment (unclosed call) is not extracted.

        has_error propagates from the broken RHS up to the assignment node,
        causing the whole node to be skipped — see design.md §Decision 3.
        """
        tree = _parse("MY_MODEL = struct(\n")
        assert extract_top_level_symbols(tree) == []

    def test_nested_assignment_not_extracted(self) -> None:
        """Scenario: Nested assignment (inside function body) is not extracted."""
        tree = _parse("def f():\n    inner = 1\n")
        assert "inner" not in extract_top_level_symbols(tree)

    def test_underscore_prefixed_name_excluded(self) -> None:
        """Scenario: Underscore-prefixed name is excluded."""
        tree = _parse("_PRIVATE = 1\n")
        assert extract_top_level_symbols(tree) == []

    def test_empty_document_returns_empty_list(self) -> None:
        """Scenario: Empty document returns empty list."""
        tree = _parse("")
        assert extract_top_level_symbols(tree) == []

    def test_multiple_symbols_returned_in_source_order(self) -> None:
        """Scenario: Multiple symbols returned in source order."""
        tree = _parse("A = 1\nB = 2\ndef f():\n    pass\n")
        assert extract_top_level_symbols(tree) == ["A", "B", "f"]

    def test_error_sibling_does_not_suppress_clean_symbol(self) -> None:
        """Scenario: ERROR sibling does not suppress a clean adjacent symbol.

        The invalid function header ``def (`` produces an ERROR node; the
        preceding clean assignment ``A = 1`` is unaffected.
        """
        tree = _parse("A = 1\ndef (\n")
        result = extract_top_level_symbols(tree)
        assert "A" in result
        # The broken def should not appear.
        assert len(result) == 1

    def test_framework_internal_name_excluded(self) -> None:
        """Scenario: Framework internal name (from _FRAMEWORK_INTERNALS) excluded."""
        # Use __MLODY__ — a valid Starlark identifier that is in the set and
        # is not a keyword (unlike "load"), so tree-sitter parses it cleanly.
        tree = _parse("__MLODY__ = 1\n")
        assert "__MLODY__" not in extract_top_level_symbols(tree)


# ---------------------------------------------------------------------------
# Requirement: Apply incremental range-edits to the document buffer
# Spec: scenarios in lsp-incremental-sync/spec.md
# ---------------------------------------------------------------------------


def _partial(
    new_text: str,
    start_line: int,
    start_char: int,
    end_line: int,
    end_char: int,
) -> types.TextDocumentContentChangePartial:
    """Build a TextDocumentContentChangePartial for the given range."""
    return types.TextDocumentContentChangePartial(
        range=types.Range(
            start=types.Position(line=start_line, character=start_char),
            end=types.Position(line=end_line, character=end_char),
        ),
        text=new_text,
    )


def _whole(new_text: str) -> types.TextDocumentContentChangeWholeDocument:
    """Build a TextDocumentContentChangeWholeDocument."""
    return types.TextDocumentContentChangeWholeDocument(text=new_text)


class TestApplyIncrementalChanges:
    """Requirement: apply_incremental_changes applies ordered LSP range-edits."""

    def test_single_character_insertion(self) -> None:
        """Scenario: Single character insertion — insert 'X' at end of first word."""
        # Insert "X" after "abc" on line 0 (character position 3).
        result = apply_incremental_changes(
            "abc\ndef\n",
            [_partial("X", start_line=0, start_char=3, end_line=0, end_char=3)],
        )
        assert result == "abcX\ndef\n"

    def test_single_character_deletion(self) -> None:
        """Scenario: Single character deletion — delete chars 0–1 on line 0."""
        # Delete "ab" (chars 0 through 2, exclusive) on line 0 of "abc\n".
        result = apply_incremental_changes(
            "abc\n",
            [_partial("", start_line=0, start_char=0, end_line=0, end_char=2)],
        )
        assert result == "c\n"

    def test_text_replacement(self) -> None:
        """Scenario: Text replacement — replace 'old' at (0,0)–(0,3) with 'new'."""
        result = apply_incremental_changes(
            "old\n",
            [_partial("new", start_line=0, start_char=0, end_line=0, end_char=3)],
        )
        assert result == "new\n"

    def test_multi_line_replacement(self) -> None:
        """Scenario: Multi-line replacement — replace lines 1–2 with a single line."""
        original = "line0\nline1\nline2\nline3\n"
        # Replace "line1\nline2" (lines 1 and 2, from char 0 to end of line 2)
        # with a single word.
        result = apply_incremental_changes(
            original,
            [_partial("REPLACED", start_line=1, start_char=0, end_line=2, end_char=5)],
        )
        # Resulting lines: line0, REPLACED, line3 — three content lines + trailing newline.
        assert result.count("\n") < original.count("\n")
        assert "REPLACED" in result
        assert "line1" not in result
        assert "line2" not in result

    def test_empty_changes_list_returns_text_unchanged(self) -> None:
        """Scenario: Empty changes list — original text returned unchanged."""
        original = "x = 1\ny = 2\n"
        result = apply_incremental_changes(original, [])
        assert result == original

    def test_whole_document_fallback_replaces_text(self) -> None:
        """Scenario: Whole-document fallback — TextDocumentContentChangeWholeDocument
        replaces text wholesale."""
        result = apply_incremental_changes(
            "old content\n",
            [_whole("brand new content\n")],
        )
        assert result == "brand new content\n"

    def test_multiple_changes_applied_in_order(self) -> None:
        """Scenario: Multiple changes applied in order — two sequential partial changes
        in one list are both applied."""
        # Start: "abc\n"
        # Change 1: replace "a" at (0,0)–(0,1) with "X" → "Xbc\n"
        # Change 2: replace "b" at (0,1)–(0,2) (in the *updated* text) with "Y" → "XYc\n"
        result = apply_incremental_changes(
            "abc\n",
            [
                _partial("X", start_line=0, start_char=0, end_line=0, end_char=1),
                _partial("Y", start_line=0, start_char=1, end_line=0, end_char=2),
            ],
        )
        assert result == "XYc\n"
