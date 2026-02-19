"""Tests for mlody.lsp.definition — path resolution, symbol search, get_definition."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mlody.lsp.definition import (
    _extract_load_string,
    _extract_symbol_at_cursor,
    _find_symbol_line,
    _resolve_load_path,
    get_definition,
)


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
# _extract_load_string
# ---------------------------------------------------------------------------


class TestExtractLoadString:
    def test_extracts_path_when_cursor_inside(self) -> None:
        line = 'load("//mlody/core/builtins.mlody", "struct")'
        # Cursor at position 10 — inside the path string
        result = _extract_load_string(line, 10)
        assert result == "//mlody/core/builtins.mlody"

    def test_returns_none_when_cursor_outside_load(self) -> None:
        line = 'MY_CONFIG = struct(lr=0.001)'
        result = _extract_load_string(line, 5)
        assert result is None


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
# get_definition
# ---------------------------------------------------------------------------


class TestGetDefinition:
    """Requirement: Go-to-definition for load() paths and imported symbols."""

    def test_returns_none_when_evaluator_is_none(self) -> None:
        result = get_definition(
            evaluator=None,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
            line='load("//mlody/core/builtins.mlody", "struct")',
            char=10,
        )
        assert result is None

    def test_navigates_to_file_on_load_path_cursor(self, tmp_path: Path) -> None:
        target = tmp_path / "mlody" / "core" / "builtins.mlody"
        target.parent.mkdir(parents=True)
        target.write_text("")

        evaluator = MagicMock()
        evaluator._module_globals = {}

        line = 'load("//mlody/core/builtins.mlody", "struct")'
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "pipeline.mlody",
            line=line,
            char=10,
        )

        assert result is not None
        assert result.range.start.line == 0

    def test_navigates_to_symbol_definition_line(self, tmp_path: Path) -> None:
        helper = tmp_path / "helper.mlody"
        helper.write_text("x = 1\nMY_CONFIG = struct(lr=0.001)\n")

        current = tmp_path / "pipeline.mlody"
        current.write_text('load(":helper.mlody", "MY_CONFIG")\nresult = MY_CONFIG\n')

        evaluator = MagicMock()
        evaluator._module_globals = {
            current: {"MY_CONFIG": object(), "__builtins__": {}, "load": None, "__MLODY__": True, "builtins": None}
        }

        line = "result = MY_CONFIG"
        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=current,
            line=line,
            char=12,  # cursor on MY_CONFIG
        )

        assert result is not None
        assert result.range.start.line == 1  # MY_CONFIG is at line 1 (0-indexed)

    def test_returns_none_for_transitive_symbol(self, tmp_path: Path) -> None:
        # SHARED_VAR is in helper but not explicitly imported in current file.
        helper = tmp_path / "helper.mlody"
        helper.write_text("SHARED_VAR = struct()\n")

        current = tmp_path / "pipeline.mlody"
        current.write_text("# no load() for SHARED_VAR\nresult = SHARED_VAR\n")

        evaluator = MagicMock()
        # SHARED_VAR not in current file's globals (not directly imported)
        evaluator._module_globals = {
            current: {"__builtins__": {}, "load": None, "__MLODY__": True, "builtins": None}
        }

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=current,
            line="result = SHARED_VAR",
            char=12,
        )

        assert result is None

    def test_returns_none_for_builtin_symbol(self, tmp_path: Path) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {
            tmp_path / "file.mlody": {"struct": object()}
        }

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "file.mlody",
            line="x = struct(",
            char=5,  # cursor on 'struct'
        )

        assert result is None

    @pytest.mark.parametrize("char", [0, 3, 6])
    def test_returns_none_for_cursor_on_whitespace_or_punctuation(
        self, tmp_path: Path, char: int
    ) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {}

        result = get_definition(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "file.mlody",
            line="   =   ",
            char=char,
        )

        assert result is None
