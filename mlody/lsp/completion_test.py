"""Tests for mlody.lsp.completion — context detection, path resolution, completions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from common.python.starlarkish.evaluator.evaluator import SAFE_BUILTINS
from mlody.lsp.completion import (
    _builtin_member_completions,
    _detect_context,
    _general_completions,
    _load_path_completions,
    get_completions,
)
from mlody.lsp.parser import CACHE, node_at_position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context_at(src: str, line: int, char: int) -> str:
    """Parse `src`, find the node at (line, char), and return the detected context."""
    # Use a unique URI per call to avoid cross-test cache hits.
    uri = f"file:///test_completion_{line}_{char}_{hash(src)}.mlody"
    tree = CACHE.update(uri, 1, src)
    node = node_at_position(tree, line, char)
    lines = src.splitlines()
    line_to_cursor = lines[line][:char] if line < len(lines) else ""
    return _detect_context(node, line_to_cursor)


# ---------------------------------------------------------------------------
# _detect_context
# ---------------------------------------------------------------------------


class TestDetectContext:
    """Requirement: Context-aware completion source selection."""

    def test_load_path_detected(self) -> None:
        # Cursor inside the path string of a single-line load() call.
        src = 'load("//mlody/pipeline.mlody", "MY_VAR")'
        assert _context_at(src, 0, 10) == "load_path"

    def test_load_path_with_colon_prefix(self) -> None:
        src = "load(':helper.mlody', 'SYM')"
        # cursor after "load(':'" → col 7 = inside the path string
        assert _context_at(src, 0, 7) == "load_path"

    def test_load_path_multiline(self) -> None:
        # load( is on line 0; path string is on line 1.
        src = 'load(\n    "//mlody/pipeline.mlody",\n    "MY_VAR"\n)'
        # col 10 on line 1 is inside "//mlody/pipeline.mlody"
        assert _context_at(src, 1, 10) == "load_path"

    def test_load_symbol_detected(self) -> None:
        # Cursor inside the symbol string (second arg) of a load() call.
        src = 'load("//mlody/pipeline.mlody", "MY_VAR")'
        # "MY_VAR" starts at col 31; cursor at col 33 is on 'Y'
        assert _context_at(src, 0, 33) == "load_symbol"

    def test_load_symbol_multiline(self) -> None:
        # Symbol string is on its own line.
        src = 'load(\n    "//mlody/pipeline.mlody",\n    "MY_VAR"\n)'
        # line 2: '    "MY_VAR"' — col 7 is inside the string
        assert _context_at(src, 2, 7) == "load_symbol"

    def test_builtins_member_detected(self) -> None:
        src = "x = builtins."
        assert _context_at(src, 0, len("x = builtins.")) == "builtins_member"

    def test_general_for_plain_line(self) -> None:
        src = "my_var = str"
        assert _context_at(src, 0, 5) == "general"

    def test_general_for_empty_line(self) -> None:
        src = ""
        assert _context_at(src, 0, 0) == "general"


# ---------------------------------------------------------------------------
# _builtin_member_completions
# ---------------------------------------------------------------------------


class TestBuiltinMemberCompletions:
    """Requirement: Complete builtins member methods."""

    def test_returns_register_and_ctx(self) -> None:
        result = _builtin_member_completions()
        assert "register" in result
        assert "ctx" in result


# ---------------------------------------------------------------------------
# _general_completions
# ---------------------------------------------------------------------------


class TestGeneralCompletions:
    """Requirement: Complete safe builtins / Complete loaded symbols."""

    def test_includes_all_safe_builtins_for_unevaluated_file(self) -> None:
        # _module_globals won't contain the file — should still get builtins.
        evaluator = MagicMock()
        evaluator._module_globals = {}
        result = _general_completions(evaluator, Path("/repo/mlody/pipeline.mlody"))

        for key in SAFE_BUILTINS:
            assert key in result

    def test_includes_loaded_symbols(self) -> None:
        evaluator = MagicMock()
        current_file = Path("/repo/mlody/pipeline.mlody")
        evaluator._module_globals = {
            current_file: {
                "MY_CONFIG": object(),
                "__builtins__": {},       # framework — must be excluded
                "load": lambda: None,     # framework — must be excluded
                "__MLODY__": True,        # framework — must be excluded
                "builtins": object(),     # framework — must be excluded
            }
        }

        result = _general_completions(evaluator, current_file)

        assert "MY_CONFIG" in result

    def test_excludes_framework_internals(self) -> None:
        evaluator = MagicMock()
        current_file = Path("/repo/mlody/pipeline.mlody")
        evaluator._module_globals = {
            current_file: {
                "__builtins__": {},
                "load": lambda: None,
                "__MLODY__": True,
                "builtins": object(),
                "USER_VAR": 42,
            }
        }

        result = _general_completions(evaluator, current_file)

        assert "__builtins__" not in result
        assert "load" not in result
        assert "__MLODY__" not in result
        assert "builtins" not in result
        assert "USER_VAR" in result


# ---------------------------------------------------------------------------
# _load_path_completions
# ---------------------------------------------------------------------------


class TestLoadPathCompletions:
    """Requirement: Complete file paths in load() strings."""

    def test_double_slash_resolves_from_monorepo_root(self, tmp_path: Path) -> None:
        mlody_dir = tmp_path / "mlody"
        mlody_dir.mkdir()
        (mlody_dir / "pipeline.mlody").write_text("")

        result = _load_path_completions(
            partial="//mlody/",
            monorepo_root=tmp_path,
            current_file=tmp_path / "other" / "file.mlody",
        )

        assert "pipeline.mlody" in result

    def test_colon_resolves_from_current_file_directory(self, tmp_path: Path) -> None:
        current_dir = tmp_path / "teams"
        current_dir.mkdir()
        (current_dir / "helper.mlody").write_text("")
        current_file = current_dir / "pipeline.mlody"

        result = _load_path_completions(
            partial=":",
            monorepo_root=tmp_path,
            current_file=current_file,
        )

        assert "helper.mlody" in result

    def test_bare_prefix_returns_empty(self) -> None:
        result = _load_path_completions(
            partial="",
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
        )

        assert result == []


# ---------------------------------------------------------------------------
# get_completions
# ---------------------------------------------------------------------------


class TestGetCompletions:
    """Requirement: Fall back to builtins-only / No crash on workspace failure."""

    def test_returns_empty_list_when_evaluator_is_none(self) -> None:
        src = "struct("
        tree = CACHE.update("file:///gc_test1.mlody", 1, src)
        result = get_completions(
            evaluator=None,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
            tree=tree,
            line=0,
            character=7,
            document_lines=[src],
        )

        assert result == []

    def test_returns_only_builtins_when_file_not_evaluated(self) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {}  # file not in here

        src = "str"
        tree = CACHE.update("file:///gc_test2.mlody", 1, src)
        items = get_completions(
            evaluator=evaluator,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/unknown.mlody"),
            tree=tree,
            line=0,
            character=3,
            document_lines=[src],
        )

        labels = [item.label for item in items]
        for key in SAFE_BUILTINS:
            assert key in labels
        assert "__builtins__" not in labels

    def test_load_symbol_returns_empty(self) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {}

        src = 'load("//mlody/pipeline.mlody", "MY_VAR")'
        tree = CACHE.update("file:///gc_test3.mlody", 1, src)
        # Cursor inside "MY_VAR" (col 33)
        items = get_completions(
            evaluator=evaluator,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
            tree=tree,
            line=0,
            character=33,
            document_lines=[src],
        )

        assert items == []

    def test_multiline_load_path_completions(self, tmp_path: Path) -> None:
        mlody_dir = tmp_path / "mlody"
        mlody_dir.mkdir()
        (mlody_dir / "pipeline.mlody").write_text("")

        evaluator = MagicMock()
        evaluator._module_globals = {}

        src = 'load(\n    "//mlody/"\n)'
        lines = src.splitlines()
        tree = CACHE.update("file:///gc_test4.mlody", 1, src)
        # Cursor at end of "//mlody/" on line 1: col 13 (after the last '/')
        items = get_completions(
            evaluator=evaluator,
            monorepo_root=tmp_path,
            current_file=tmp_path / "other" / "file.mlody",
            tree=tree,
            line=1,
            character=13,
            document_lines=lines,
        )

        labels = [item.label for item in items]
        assert "pipeline.mlody" in labels
