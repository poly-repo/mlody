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


# ---------------------------------------------------------------------------
# _detect_context
# ---------------------------------------------------------------------------


class TestDetectContext:
    """Requirement: Context-aware completion source selection."""

    def test_load_path_detected(self) -> None:
        # Cursor inside a load("//mlody/... string
        assert _detect_context('load("//mlody/') == "load_path"

    def test_load_path_with_colon_prefix(self) -> None:
        assert _detect_context("load(':") == "load_path"

    def test_builtins_member_detected(self) -> None:
        assert _detect_context("x = builtins.") == "builtins_member"

    def test_general_for_plain_line(self) -> None:
        assert _detect_context("my_var = str") == "general"

    def test_general_for_empty_line(self) -> None:
        assert _detect_context("") == "general"


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
        # Set up: monorepo_root/mlody/pipeline.mlody exists
        mlody_dir = tmp_path / "mlody"
        mlody_dir.mkdir()
        (mlody_dir / "pipeline.mlody").write_text("")

        result = _load_path_completions(
            line='load("//mlody/',
            monorepo_root=tmp_path,
            current_file=tmp_path / "other" / "file.mlody",
        )

        assert "pipeline.mlody" in result

    def test_colon_resolves_from_current_file_directory(self, tmp_path: Path) -> None:
        # Set up sibling files
        current_dir = tmp_path / "teams"
        current_dir.mkdir()
        (current_dir / "helper.mlody").write_text("")
        current_file = current_dir / "pipeline.mlody"

        result = _load_path_completions(
            line="load(':",
            monorepo_root=tmp_path,
            current_file=current_file,
        )

        assert "helper.mlody" in result

    def test_bare_prefix_returns_empty(self) -> None:
        # load("  — no // or : prefix yet
        result = _load_path_completions(
            line='load("',
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
        result = get_completions(
            evaluator=None,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/file.mlody"),
            line="struct(",
        )

        assert result == []

    def test_returns_only_builtins_when_file_not_evaluated(self) -> None:
        evaluator = MagicMock()
        evaluator._module_globals = {}  # file not in here

        items = get_completions(
            evaluator=evaluator,
            monorepo_root=Path("/repo"),
            current_file=Path("/repo/mlody/unknown.mlody"),
            line="str",
        )

        labels = [item.label for item in items]
        # All safe builtins should be present
        for key in SAFE_BUILTINS:
            assert key in labels
        # No framework internals
        assert "__builtins__" not in labels
