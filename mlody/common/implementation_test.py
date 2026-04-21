"""Integration tests for mlody/common/build_ref.mlody and implementation.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_BUILD_REF_MLODY = (_THIS_DIR / "build_ref.mlody").read_text()
_IMPLEMENTATION_MLODY = (_THIS_DIR / "implementation.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()
_ACTION_MLODY = (_THIS_DIR / "action.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/build_ref.mlody": _BUILD_REF_MLODY,
    "mlody/common/implementation.mlody": _IMPLEMENTATION_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
}

# Both build_ref and implementation are loaded explicitly; their top-level
# declarations (bazel, container, shell_script, system_binary) are injected
# into scope via builtins.inject — no explicit import of those names is needed.
_PREAMBLE = (
    'load("//mlody/common/build_ref.mlody")\n'
    'load("//mlody/common/implementation.mlody")\n'
)

_PREAMBLE_WITH_ACTION = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/locations.mlody")\n'
    'load("//mlody/common/values.mlody")\n'
    'load("//mlody/common/implementation.mlody")\n'
    'load("//mlody/common/action.mlody")\n'
)


def _eval(extra_mlody: str) -> Evaluator:
    """Evaluate a snippet that has access to both build_ref and implementation."""
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def _eval_with_action(extra_mlody: str) -> Evaluator:
    """Evaluate a snippet that also loads action.mlody (for TC-018/TC-019)."""
    script = _PREAMBLE_WITH_ACTION + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    return ev


def _get(ev: Evaluator, name: str) -> object:
    return ev._module_globals[ev.root_path / "test.mlody"][name]


# ---------------------------------------------------------------------------
# TC-001: bazel(target="//x:y") returns correct struct fields
# ---------------------------------------------------------------------------


def test_bazel_returns_struct_with_kind_build_ref() -> None:
    """TC-001: bazel(target='//x:y') returns kind='build_ref'."""
    ev = _eval('result = bazel(target="//x:y")')
    assert _get(ev, "result").kind == "build_ref"  # type: ignore[union-attr]


def test_bazel_returns_struct_with_type_bazel() -> None:
    """TC-001: bazel(target='//x:y') returns type='bazel'."""
    ev = _eval('result = bazel(target="//x:y")')
    assert _get(ev, "result").type == "bazel"  # type: ignore[union-attr]


def test_bazel_returns_struct_with_target_field() -> None:
    """TC-001: bazel(target='//x:y') stores target='//x:y'."""
    ev = _eval('result = bazel(target="//x:y")')
    assert _get(ev, "result").target == "//x:y"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-002: bazel() with no arguments raises TypeError
# ---------------------------------------------------------------------------


def test_bazel_no_args_raises_type_error() -> None:
    """TC-002: bazel() raises TypeError because target is mandatory."""
    with pytest.raises(TypeError):
        _eval("result = bazel()")


# ---------------------------------------------------------------------------
# TC-003: bazel(target="not-a-label") raises ValueError
# ---------------------------------------------------------------------------


def test_bazel_invalid_target_raises_value_error() -> None:
    """TC-003: bazel(target='not-a-label') raises ValueError (invalid prefix)."""
    with pytest.raises(ValueError):
        _eval('result = bazel(target="not-a-label")')


# ---------------------------------------------------------------------------
# TC-004: bazel accepts both "//" and ":" prefixed targets
# ---------------------------------------------------------------------------


def test_bazel_accepts_absolute_target() -> None:
    """TC-004: bazel(target='//valid:target') is accepted."""
    ev = _eval('result = bazel(target="//valid:target")')
    assert _get(ev, "result").target == "//valid:target"  # type: ignore[union-attr]


def test_bazel_accepts_local_target() -> None:
    """TC-004: bazel(target=':local') is accepted."""
    ev = _eval('result = bazel(target=":local")')
    assert _get(ev, "result").target == ":local"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-005: container(build=bazel(...)) returns correct struct fields
# ---------------------------------------------------------------------------


def test_container_returns_struct_with_kind_implementation() -> None:
    """TC-005: container(build=bazel(...)) returns kind='implementation'."""
    ev = _eval('result = container(build=bazel(target="//x:y"))')
    assert _get(ev, "result").kind == "implementation"  # type: ignore[union-attr]


def test_container_returns_struct_with_type_container() -> None:
    """TC-005: container(build=bazel(...)) returns type='container'."""
    ev = _eval('result = container(build=bazel(target="//x:y"))')
    assert _get(ev, "result").type == "container"  # type: ignore[union-attr]


def test_container_stores_build_ref_struct() -> None:
    """TC-005: container stores the build_ref struct in the build field."""
    ev = _eval('result = container(build=bazel(target="//x:y"))')
    build = _get(ev, "result").build  # type: ignore[union-attr]
    assert build.kind == "build_ref"  # type: ignore[union-attr]
    assert build.type == "bazel"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-006: container() with no build argument raises TypeError
# ---------------------------------------------------------------------------


def test_container_no_args_raises_type_error() -> None:
    """TC-006: container() raises TypeError because build is mandatory."""
    with pytest.raises(TypeError):
        _eval("result = container()")


# ---------------------------------------------------------------------------
# TC-007: container(build="not-a-struct") raises TypeError
# ---------------------------------------------------------------------------


def test_container_non_struct_build_raises_type_error() -> None:
    """TC-007: container(build='not-a-struct') raises TypeError."""
    with pytest.raises(TypeError):
        _eval('result = container(build="not-a-struct")')


# ---------------------------------------------------------------------------
# TC-008: shell_script(content="echo hi") returns valid struct
# ---------------------------------------------------------------------------


def test_shell_script_with_content_returns_valid_struct() -> None:
    """TC-008: shell_script(content='echo hi') returns kind='implementation'."""
    ev = _eval('result = shell_script(content="echo hi")')
    r = _get(ev, "result")
    assert r.kind == "implementation"  # type: ignore[union-attr]
    assert r.type == "shell_script"  # type: ignore[union-attr]


def test_shell_script_with_content_has_file_none() -> None:
    """TC-008: shell_script(content='echo hi') has file=None."""
    ev = _eval('result = shell_script(content="echo hi")')
    assert _get(ev, "result").file is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-009: shell_script(file="scripts/run.sh") returns valid struct
# ---------------------------------------------------------------------------


def test_shell_script_with_file_returns_valid_struct() -> None:
    """TC-009: shell_script(file='scripts/run.sh') returns kind='implementation'."""
    ev = _eval('result = shell_script(file="scripts/run.sh")')
    r = _get(ev, "result")
    assert r.kind == "implementation"  # type: ignore[union-attr]
    assert r.type == "shell_script"  # type: ignore[union-attr]


def test_shell_script_with_file_has_content_none() -> None:
    """TC-009: shell_script(file='scripts/run.sh') has content=None."""
    ev = _eval('result = shell_script(file="scripts/run.sh")')
    assert _get(ev, "result").content is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-010: shell_script(content="...", file="...") raises ValueError
# ---------------------------------------------------------------------------


def test_shell_script_both_content_and_file_raises_value_error() -> None:
    """TC-010: providing both content and file raises ValueError."""
    with pytest.raises(ValueError):
        _eval('result = shell_script(content="echo hi", file="run.sh")')


# ---------------------------------------------------------------------------
# TC-011: shell_script() with neither content nor file raises ValueError
# ---------------------------------------------------------------------------


def test_shell_script_no_content_no_file_raises_value_error() -> None:
    """TC-011: shell_script() with neither content nor file raises ValueError."""
    with pytest.raises(ValueError):
        _eval("result = shell_script()")


# ---------------------------------------------------------------------------
# TC-012: shell_script(content="...", interpreter="/bin/bash") stores interpreter
# ---------------------------------------------------------------------------


def test_shell_script_stores_interpreter() -> None:
    """TC-012: shell_script with interpreter stores it correctly."""
    ev = _eval('result = shell_script(content="echo hi", interpreter="/bin/bash")')
    assert _get(ev, "result").interpreter == "/bin/bash"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-013: shell_script(file="/absolute/path.sh") raises ValueError
# ---------------------------------------------------------------------------


def test_shell_script_absolute_file_raises_value_error() -> None:
    """TC-013: shell_script with absolute file path raises ValueError."""
    with pytest.raises(ValueError):
        _eval('result = shell_script(file="/absolute/path.sh")')


# ---------------------------------------------------------------------------
# TC-014: shell_script(file="../escape.sh") raises ValueError
# ---------------------------------------------------------------------------


def test_shell_script_parent_traversal_in_file_raises_value_error() -> None:
    """TC-014: shell_script with '..' component in file raises ValueError."""
    with pytest.raises(ValueError):
        _eval('result = shell_script(file="../escape.sh")')


# ---------------------------------------------------------------------------
# TC-015: system_binary(path="/usr/bin/ffmpeg") returns struct with absolute path
# ---------------------------------------------------------------------------


def test_system_binary_with_absolute_path_returns_struct() -> None:
    """TC-015: system_binary(path='/usr/bin/ffmpeg') returns kind='implementation'."""
    ev = _eval('result = system_binary(path="/usr/bin/ffmpeg")')
    r = _get(ev, "result")
    assert r.kind == "implementation"  # type: ignore[union-attr]
    assert r.type == "system_binary"  # type: ignore[union-attr]
    assert r.path == "/usr/bin/ffmpeg"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TC-016: system_binary(path="relative/bin") raises ValueError
# ---------------------------------------------------------------------------


def test_system_binary_relative_path_raises_value_error() -> None:
    """TC-016: system_binary with relative path raises ValueError."""
    with pytest.raises(ValueError):
        _eval('result = system_binary(path="relative/bin")')


# ---------------------------------------------------------------------------
# TC-017: system_binary(path="//bazel:target") raises ValueError
# ---------------------------------------------------------------------------


def test_system_binary_bazel_label_raises_value_error() -> None:
    """TC-017: system_binary with Bazel label raises ValueError (not an abs path)."""
    with pytest.raises(ValueError):
        _eval('result = system_binary(path="//bazel:target")')


# ---------------------------------------------------------------------------
# TC-018: action(implementation=container(...)) stores implementation struct
# ---------------------------------------------------------------------------


def test_action_with_container_implementation_stores_struct() -> None:
    """TC-018: action implementation field holds a container struct."""
    ev = _eval_with_action(
        'value(name="out", type=integer(), location=s3())\n'
        'action(\n'
        '  name="train",\n'
        '  outputs=["out"],\n'
        '  implementation=container(build=bazel(target="//x:img")),\n'
        ')\n'
    )
    a = ev._actions_by_name["train"]
    assert a.implementation.kind == "implementation"
    assert a.implementation.type == "container"


# ---------------------------------------------------------------------------
# TC-019: action(implementation=["old", "string", "list"]) raises TypeError
# ---------------------------------------------------------------------------


def test_action_with_string_list_implementation_raises_type_error() -> None:
    """TC-019: passing a string_list for implementation raises TypeError."""
    with pytest.raises(TypeError):
        _eval_with_action(
            'value(name="out", type=integer(), location=s3())\n'
            'action(name="old", outputs=["out"], implementation=["old", "string", "list"])\n'
        )
