"""Tests for mlody.core.source_parser — treesitter entity range extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlody.core.source_parser import extract_entity_ranges

_FAKE_PATH = Path("/fake/models.mlody")


def test_direct_register_single() -> None:
    source = 'builtins.register("root", struct(name="lexica", path="//mlody/teams/lexica"))\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {("root", "lexica"): (1, 1)}


def test_direct_register_multiline() -> None:
    source = """\
builtins.register("type", struct(
    kind="type",
    name="my-type",
    attributes={},
))
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {("type", "my-type"): (1, 5)}


def test_helper_call_root() -> None:
    source = 'root("lexica", path="//mlody/teams/lexica")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {("root", "lexica"): (1, 1)}


def test_helper_call_task() -> None:
    source = 'task("train-bert", description="Train BERT model")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {("task", "train-bert"): (1, 1)}


def test_helper_call_action() -> None:
    source = 'action("run-eval", description="Evaluate")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {("action", "run-eval"): (1, 1)}


def test_cached_value_contributes_local_and_remote_value_ranges() -> None:
    source = """\
cached_value(
    name="employees",
    source=remote(uri="https://example.com/employees.csv"),
    location=posix(path="~/.cache/mlody/employees.csv"),
)
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {
        ("value", "employees"): (1, 5),
        ("value", "employees-remote"): (1, 5),
    }


def test_cached_value_uses_literal_remote_name_override() -> None:
    source = """\
cached_value(
    name="employees",
    remote_name="employees-origin",
    source=remote(uri="https://example.com/employees.csv"),
    location=posix(path="~/.cache/mlody/employees.csv"),
)
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {
        ("value", "employees"): (1, 6),
        ("value", "employees-origin"): (1, 6),
    }


def test_multiple_entities() -> None:
    source = """\
root("lexica", path="//mlody/teams/lexica")
task("train", description="Training task")
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {
        ("root", "lexica"): (1, 1),
        ("task", "train"): (2, 2),
    }


def test_computed_name_skipped() -> None:
    """Entities with computed (non-literal) names are silently skipped."""
    source = 'root(my_name, path="//foo")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {}


def test_non_registration_calls_ignored() -> None:
    source = 'print("hello")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {}


def test_assignment_statement_ignored() -> None:
    """Assignments are not registration calls — they should not appear in result."""
    source = 'MY_MODEL = struct(name="bert")\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {}


def test_error_nodes_skipped() -> None:
    """Valid nodes before a syntax error are still extracted; broken nodes are skipped."""
    source = """\
root("valid", path="//foo")
@@@invalid syntax@@@
"""
    result = extract_entity_ranges(_fake_path(), source)
    # The valid root call before the broken line must be present.
    assert ("root", "valid") in result


def test_line_numbers_across_file() -> None:
    source = """\
load("//mlody/core/builtins.mlody", "root")

root("lexica", path="//mlody/teams/lexica")

builtins.register("type", struct(
    name="my-type",
    kind="type",
    attributes={},
))
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result[("root", "lexica")] == (3, 3)
    assert result[("type", "my-type")] == (5, 9)


def test_nested_call_as_keyword_arg() -> None:
    """Nested registration call passed as keyword arg is captured (FR-001, FR-002)."""
    source = 'task(name="t", action=action(name="a", inputs=[]))\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert ("task", "t") in result
    assert ("action", "a") in result


def test_nested_call_as_positional_arg() -> None:
    """Nested registration call passed as positional arg is captured."""
    source = 'task("t", action("a", inputs=[]))\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert ("task", "t") in result
    assert ("action", "a") in result


def test_call_inside_function_body() -> None:
    """Registration call inside a def body is captured via full-AST walk (FR-001)."""
    source = """\
def mk():
    task("t", description="inside a def")
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert ("task", "t") in result


def test_keyword_name_arg() -> None:
    """name= keyword argument is used as the entity name when no positional string (FR-002)."""
    source = 'task(name="train", inputs=[])\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert ("task", "train") in result


def test_computed_name_no_entry() -> None:
    """A call whose name argument is not a literal string produces no entry."""
    source = 'task(get_name(), inputs=[])\n'
    result = extract_entity_ranges(_fake_path(), source)
    assert result == {}


def test_multiple_nested_entities() -> None:
    """Multiple nested action() calls inside one task() are all captured."""
    source = """\
task(
    name="train",
    action=action(name="run", inputs=[]),
    cleanup=action(name="clean", inputs=[]),
)
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert ("task", "train") in result
    assert ("action", "run") in result
    assert ("action", "clean") in result


def test_duplicate_kind_name_raises() -> None:
    """Duplicate (kind, name) within the same file raises ValueError (FR-008)."""
    source = """\
task("train", description="top-level")

def mk():
    task("train", description="inside def")
"""
    with pytest.raises(ValueError) as exc_info:
        extract_entity_ranges(_fake_path(), source)
    msg = str(exc_info.value)
    assert "task" in msg
    assert "train" in msg
    assert str(_FAKE_PATH) in msg


def test_keyword_name_arg_line_numbers() -> None:
    """Line numbers are correct for a multiline call using name= keyword form."""
    source = """\
task(
    name="train",
    inputs=[],
)
"""
    result = extract_entity_ranges(_fake_path(), source)
    assert result[("task", "train")] == (1, 4)


def _fake_path() -> Path:
    return _FAKE_PATH
