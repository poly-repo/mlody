"""Tests for mlody.lsp.definition — path resolution, symbol search, get_definition."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import tree_sitter

from mlody.lsp.definition import (
    _extract_symbol_at_cursor,
    _find_symbol_line,
    _resolve_load_path,
    get_definition,
)
from mlody.lsp.parser import DocumentCache


def _parse(source: str) -> tree_sitter.Tree:
    """Parse Starlark source text and return a tree-sitter Tree.

    Uses a local DocumentCache instance to avoid sharing state between tests.
    """
    cache = DocumentCache()
    return cache.update("test://test.mlody", 0, source)


# ---------------------------------------------------------------------------
# _resolve_load_path
# ---------------------------------------------------------------------------


class TestResolveLoadPath:
    """Requirement: Go-to-definition for load() file paths."""

    def test_double_slash_resolves_from_monorepo_root(self, tmp_path: Path) -> None:
        target = tmp_path / "mlody" / "core" / "builtins.mlody"
        target.parent.mkdir(parents=True)
        target.write_text("")

        result = _resolve_load_path(
            "//mlody/core/builtins.mlody",
            monorepo_root=tmp_path,
            current_file=tmp_path / "some" / "file.mlody",
        )

        assert result == target.resolve()

    def test_colon_resolves_from_current_file_directory(self, tmp_path: Path) -> None:
        helper = tmp_path / "helper.mlody"
        helper.write_text("")
        current_file = tmp_path / "pipeline.mlody"

        result = _resolve_load_path(
            ":helper.mlody",
            monorepo_root=Path("/repo"),
            current_file=current_file,
        )

        assert result == helper.resolve()

    def test_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        result = _resolve_load_path(
            "//mlody/missing.mlody",
            monorepo_root=tmp_path,
            current_file=tmp_path / "file.mlody",
        )

        assert result is None


# ---------------------------------------------------------------------------
# _find_symbol_line
# ---------------------------------------------------------------------------


class TestFindSymbolLine:
    """Requirement: Go-to-definition for imported symbols."""

    def test_finds_assignment_line(self, tmp_path: Path) -> None:
        src = tmp_path / "helper.mlody"
        src.write_text("x = 1\nMY_CONFIG = struct(lr=0.001)\nz = 2\n")

        assert _find_symbol_line(src, "MY_CONFIG") == 1

    def test_finds_def_line(self, tmp_path: Path) -> None:
        src = tmp_path / "helper.mlody"
        src.write_text("x = 1\ndef my_func():\n    pass\n")

        assert _find_symbol_line(src, "my_func") == 1

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        src = tmp_path / "helper.mlody"
        src.write_text("x = 1\n")

        assert _find_symbol_line(src, "MISSING") is None


# ---------------------------------------------------------------------------
# _extract_symbol_at_cursor
# ---------------------------------------------------------------------------


class TestExtractSymbolAtCursor:
    def test_extracts_identifier(self) -> None:
        line = "result = MY_CONFIG.lr"
        # Cursor on 'MY_CONFIG' (starts at index 9)
        assert _extract_symbol_at_cursor(line, 11) == "MY_CONFIG"

    def test_returns_none_on_whitespace(self) -> None:
        line = "x = y"
        assert _extract_symbol_at_cursor(line, 3) is None


# ---------------------------------------------------------------------------
# get_definition — single-line load() scenarios
# ---------------------------------------------------------------------------


class TestGetDefinition:
    """Requirement: Go-to-definition for load() paths and imported symbols."""

    def test_returns_none_when_evaluator_is_none(self) -> None:
        source = 'load("//mlody/core/builtins.mlody", "struct")'
        tree = _parse(source)

        result = get_definition(
            evaluator=None,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
            tree=tree,
            line=0,
            char=10,
            document_lines=[source],
        )
        assert result is None

    def test_navigates_to_file_on_load_path_cursor(self, tmp_path: Path) -> None:
        target = tmp_path / "mlody" / "core" / "builtins.mlody"
        target.parent.mkdir(parents=True)
        target.write_text("")

        evaluator = MagicMock()
        evaluator._module_globals = {}

        source = 'load("//mlody/core/builtins.mlody", "struct")'
        tree = _parse(source)

        # char=10 falls inside "//mlody/core/builtins.mlody"
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "pipeline.mlody",
            tree=tree,
            line=0,
            char=10,
            document_lines=[source],
        )

        assert result is not None
        assert result.range.start.line == 0

    def test_navigates_to_symbol_definition_line(self, tmp_path: Path) -> None:
        helper = tmp_path / "helper.mlody"
        helper.write_text("x = 1\nMY_CONFIG = struct(lr=0.001)\n")

        current = tmp_path / "pipeline.mlody"
        file_source = 'load(":helper.mlody", "MY_CONFIG")\nresult = MY_CONFIG\n'
        current.write_text(file_source)

        evaluator = MagicMock()
        evaluator._module_globals = {
            current: {"MY_CONFIG": object(), "__builtins__": {}, "load": None, "__MLODY__": True, "builtins": None}
        }

        tree = _parse(file_source)
        document_lines = file_source.splitlines()

        # line=1, char=12 — cursor on MY_CONFIG identifier
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=current,
            tree=tree,
            line=1,
            char=12,
            document_lines=document_lines,
        )

        assert result is not None
        assert result.range.start.line == 1  # MY_CONFIG is at line 1 (0-indexed)

    def test_returns_none_for_transitive_symbol(self, tmp_path: Path) -> None:
        # SHARED_VAR is in helper but not explicitly imported in current file.
        helper = tmp_path / "helper.mlody"
        helper.write_text("SHARED_VAR = struct()\n")

        current = tmp_path / "pipeline.mlody"
        file_source = "# no load() for SHARED_VAR\nresult = SHARED_VAR\n"
        current.write_text(file_source)

        evaluator = MagicMock()
        # SHARED_VAR not in current file's globals (not directly imported)
        evaluator._module_globals = {
            current: {"__builtins__": {}, "load": None, "__MLODY__": True, "builtins": None}
        }

        tree = _parse(file_source)
        document_lines = file_source.splitlines()

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=current,
            tree=tree,
            line=1,
            char=12,
            document_lines=document_lines,
        )

        assert result is None

    def test_returns_none_for_builtin_symbol(self, tmp_path: Path) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {
            tmp_path / "file.mlody": {"struct": object()}
        }

        source = "x = struct("
        tree = _parse(source)

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "file.mlody",
            tree=tree,
            line=0,
            char=5,  # cursor on 'struct'
            document_lines=[source],
        )

        assert result is None

    @pytest.mark.parametrize("char", [0, 3, 6])
    def test_returns_none_for_cursor_on_whitespace_or_punctuation(
        self, tmp_path: Path, char: int
    ) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {}

        source = "   =   "
        tree = _parse(source)

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "file.mlody",
            tree=tree,
            line=0,
            char=char,
            document_lines=[source],
        )

        assert result is None


# ---------------------------------------------------------------------------
# get_definition — multi-line load() scenarios
# ---------------------------------------------------------------------------


class TestGetDefinitionMultiline:
    """Requirement: Multi-line load() path and symbol navigation.

    Covers scenarios where the path string or symbol string is on a different
    line from the load() opening — the regex approach would silently return
    None for these; the parse-tree approach handles them correctly.
    """

    def test_multiline_load_path_navigation(self, tmp_path: Path) -> None:
        """Multi-line load() path string navigates to the correct file."""
        target = tmp_path / "mlody" / "core" / "builtins.mlody"
        target.parent.mkdir(parents=True)
        target.write_text("")

        evaluator = MagicMock()
        evaluator._module_globals = {}

        # Path string on its own line (line 1 in 0-indexed terms).
        source = 'load(\n    "//mlody/core/builtins.mlody",\n    "struct"\n)'
        tree = _parse(source)
        document_lines = source.splitlines()

        # Cursor on line 1, char 10 — inside "//mlody/core/builtins.mlody".
        # Line 1: '    "//mlody/core/builtins.mlody",'
        #          0123456789...  char 10 is after the opening quote.
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "pipeline.mlody",
            tree=tree,
            line=1,
            char=10,
            document_lines=document_lines,
        )

        assert result is not None
        assert result.range.start.line == 0

    def test_multiline_load_symbol_navigation(self, tmp_path: Path) -> None:
        """Multi-line load() symbol string navigates to the symbol's definition."""
        helper = tmp_path / "helper.mlody"
        helper.write_text("x = 1\nMY_CONFIG = struct(lr=0.001)\n")

        current = tmp_path / "pipeline.mlody"
        # Symbol string on its own line (line 2 in 0-indexed terms).
        file_source = 'load(\n    ":helper.mlody",\n    "MY_CONFIG"\n)\nresult = MY_CONFIG\n'
        current.write_text(file_source)

        evaluator = MagicMock()
        evaluator._module_globals = {
            current: {
                "MY_CONFIG": object(),
                "__builtins__": {},
                "load": None,
                "__MLODY__": True,
                "builtins": None,
            }
        }

        tree = _parse(file_source)
        document_lines = file_source.splitlines()

        # Cursor on line 2, char 6 — inside "MY_CONFIG".
        # Line 2: '    "MY_CONFIG"'
        #          0123456...  char 6 is 'Y' inside MY_CONFIG.
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=current,
            tree=tree,
            line=2,
            char=6,
            document_lines=document_lines,
        )

        assert result is not None
        assert result.range.start.line == 1  # MY_CONFIG defined at line 1 in helper.mlody
