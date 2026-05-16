"""Tests for mlody.resolver.render — dom_for singledispatch registry.

Task 2.18 (mlody-refactor-phase-3):
  Verify that dom_for dispatches correctly for each registered MlodyValue
  subtype and that the fallback for unregistered types returns a text node.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from common.python.console import (
    PanelNode,
    RichDomExecutor,
    SyntaxNode,
    TreeNode,
)
from mlody.resolver.render import dom_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(node: object) -> str:
    """Render a RichDomNode to a plain string via RichDomExecutor."""
    console = Console(record=True, width=120)
    RichDomExecutor(console=console).render(node)  # type: ignore[arg-type]
    return console.export_text()


def _make_struct(**kwargs: object) -> object:
    from common.python.starlarkish.core.struct import Struct

    return Struct(**kwargs)


# ---------------------------------------------------------------------------
# MlodyWorkspaceValue
# ---------------------------------------------------------------------------


class TestDomForWorkspace:
    """dom_for(MlodyWorkspaceValue) → PanelNode with workspace title."""

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.structural import MlodyWorkspaceValue

        v = MlodyWorkspaceValue(name="lexica", root="/repo")
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_title_contains_workspace_name(self) -> None:
        from mlody.resolver.values.structural import MlodyWorkspaceValue

        v = MlodyWorkspaceValue(name="lexica", root="/repo")
        rendered = _render(dom_for(v))
        assert "lexica" in rendered

    def test_panel_contains_root_path(self) -> None:
        from mlody.resolver.values.structural import MlodyWorkspaceValue

        v = MlodyWorkspaceValue(name="lexica", root="/repo/monorepo")
        rendered = _render(dom_for(v))
        assert "/repo/monorepo" in rendered

    def test_none_name_renders_cwd(self) -> None:
        from mlody.resolver.values.structural import MlodyWorkspaceValue

        v = MlodyWorkspaceValue(name=None, root="/repo")
        rendered = _render(dom_for(v))
        assert "(cwd)" in rendered


# ---------------------------------------------------------------------------
# MlodyFolderValue
# ---------------------------------------------------------------------------


class TestDomForFolder:
    """dom_for(MlodyFolderValue) → TreeNode showing folder children."""

    def test_returns_tree_node(self) -> None:
        from mlody.resolver.values.structural import MlodyFolderValue

        v = MlodyFolderValue(path="teams/lexica", children=["models", "data"])
        assert isinstance(dom_for(v), TreeNode)

    def test_renders_children(self) -> None:
        from mlody.resolver.values.structural import MlodyFolderValue

        v = MlodyFolderValue(path="teams/lexica", children=["models", "data"])
        rendered = _render(dom_for(v))
        assert "models" in rendered
        assert "data" in rendered

    def test_empty_children_renders_empty_marker(self) -> None:
        from mlody.resolver.values.structural import MlodyFolderValue

        v = MlodyFolderValue(path="teams/empty", children=[])
        rendered = _render(dom_for(v))
        assert "(empty)" in rendered


# ---------------------------------------------------------------------------
# MlodySourceValue
# ---------------------------------------------------------------------------


class TestDomForSource:
    """dom_for(MlodySourceValue) → PanelNode for source file."""

    def test_returns_panel_node_when_abs_path_is_none(self) -> None:
        from mlody.resolver.values.structural import MlodySourceValue

        v = MlodySourceValue(path="teams/lexica/models", abs_path=None)
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_title_contains_path_with_mlody_suffix(self) -> None:
        from mlody.resolver.values.structural import MlodySourceValue

        v = MlodySourceValue(path="teams/lexica/models", abs_path=None)
        rendered = _render(dom_for(v))
        assert "teams/lexica/models.mlody" in rendered

    def test_renders_file_content_when_abs_path_exists(self, tmp_path: Path) -> None:
        from mlody.resolver.values.structural import MlodySourceValue

        mlody_file = tmp_path / "models.mlody"
        mlody_file.write_text("task(name='my_task')\n")
        v = MlodySourceValue(path="teams/lexica/models", abs_path=mlody_file)
        rendered = _render(dom_for(v))
        assert "my_task" in rendered


# ---------------------------------------------------------------------------
# MlodyTaskValue
# ---------------------------------------------------------------------------


class TestDomForTask:
    """dom_for(MlodyTaskValue) → PanelNode with task title."""

    def _make_task(self, name: str = "my_task") -> object:
        return _make_struct(
            kind="task",
            name=name,
            inputs={},
            outputs={},
            config={},
        )

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyTaskValue

        v = MlodyTaskValue(struct=self._make_task())
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_title_contains_task_name(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyTaskValue

        v = MlodyTaskValue(struct=self._make_task("training_run"))
        rendered = _render(dom_for(v))
        assert "task: training_run" in rendered


# ---------------------------------------------------------------------------
# MlodyActionValue
# ---------------------------------------------------------------------------


class TestDomForAction:
    """dom_for(MlodyActionValue) → PanelNode with action title."""

    def _make_action(self, name: str = "my_action") -> object:
        return _make_struct(
            kind="action",
            name=name,
            inputs={},
            outputs={},
            config={},
        )

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyActionValue

        v = MlodyActionValue(struct=self._make_action())
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_title_contains_action_name(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyActionValue

        v = MlodyActionValue(struct=self._make_action("deploy"))
        rendered = _render(dom_for(v))
        assert "action: deploy" in rendered


# ---------------------------------------------------------------------------
# MlodyUserValue
# ---------------------------------------------------------------------------


class TestDomForUser:
    """dom_for(MlodyUserValue) → PanelNode with user details."""

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyUserValue

        v = MlodyUserValue(struct=_make_struct(kind="user", name="alice", description="ML Lead", groups=[]))
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_title_contains_user_name(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyUserValue

        v = MlodyUserValue(struct=_make_struct(kind="user", name="alice", description="", groups=[]))
        rendered = _render(dom_for(v))
        assert "user: alice" in rendered

    def test_groups_rendered_as_comma_separated(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyUserValue

        v = MlodyUserValue(struct=_make_struct(kind="user", name="alice", description="", groups=["ml", "eng"]))
        rendered = _render(dom_for(v))
        assert "ml" in rendered
        assert "eng" in rendered

    def test_empty_groups_renders_none_marker(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyUserValue

        v = MlodyUserValue(struct=_make_struct(kind="user", name="bob", description="", groups=[]))
        rendered = _render(dom_for(v))
        assert "(none)" in rendered


# ---------------------------------------------------------------------------
# MlodyValueValue
# ---------------------------------------------------------------------------


class TestDomForValueValue:
    """dom_for(MlodyValueValue) → PanelNode with syntax-highlighted struct repr."""

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.registry_backed import MlodyValueValue

        v = MlodyValueValue(struct=_make_struct(kind="value", name="x", type=None, location=None, default=None))
        assert isinstance(dom_for(v), PanelNode)


# ---------------------------------------------------------------------------
# MlodyUnresolvedValue
# ---------------------------------------------------------------------------


class TestDomForUnresolved:
    """dom_for(MlodyUnresolvedValue) → PanelNode with red border and reason."""

    def test_returns_panel_node(self) -> None:
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        v = MlodyUnresolvedValue(label=None, reason="entity not found")  # type: ignore[arg-type]
        assert isinstance(dom_for(v), PanelNode)

    def test_panel_contains_reason(self) -> None:
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        v = MlodyUnresolvedValue(label=None, reason="entity not found")  # type: ignore[arg-type]
        rendered = _render(dom_for(v))
        assert "entity not found" in rendered


# ---------------------------------------------------------------------------
# MlodySourceRangeValue
# ---------------------------------------------------------------------------


class TestDomForSourceRange:
    """dom_for(MlodySourceRangeValue) → SyntaxNode with file path and lines."""

    def test_returns_syntax_node(self, tmp_path: Path) -> None:
        from mlody.resolver.values.structural import MlodySourceRangeValue

        f = tmp_path / "entities.mlody"
        f.write_text("line1\nline2\nline3\n")
        v = MlodySourceRangeValue(filepath="teams/myroot/entities.mlody", abs_path=f, start_line=2, end_line=3)
        assert isinstance(dom_for(v), SyntaxNode)

    def test_rendered_header_contains_path_and_line_range(self, tmp_path: Path) -> None:
        from mlody.resolver.values.structural import MlodySourceRangeValue

        f = tmp_path / "entities.mlody"
        f.write_text("line1\nline2\nline3\n")
        v = MlodySourceRangeValue(filepath="teams/myroot/entities.mlody", abs_path=f, start_line=2, end_line=3)
        rendered = _render(dom_for(v))
        assert "teams/myroot/entities.mlody:2-3" in rendered

    def test_single_line_span_has_no_dash(self, tmp_path: Path) -> None:
        from mlody.resolver.values.structural import MlodySourceRangeValue

        f = tmp_path / "entities.mlody"
        f.write_text("line1\nline2\nline3\n")
        v = MlodySourceRangeValue(filepath="teams/myroot/entities.mlody", abs_path=f, start_line=2, end_line=2)
        rendered = _render(dom_for(v))
        assert "entities.mlody:2" in rendered
        assert "entities.mlody:2-2" not in rendered


# ---------------------------------------------------------------------------
# _RawAttrValue
# ---------------------------------------------------------------------------


class TestDomForRawAttr:
    """dom_for(_RawAttrValue) → text node with str(value)."""

    def test_renders_value_as_string(self) -> None:
        from mlody.resolver.values.internal import _RawAttrValue

        v = _RawAttrValue(value=42, label=None)  # type: ignore[arg-type]
        rendered = _render(dom_for(v))
        assert "42" in rendered


# ---------------------------------------------------------------------------
# Fallback for unregistered type
# ---------------------------------------------------------------------------


class TestDomForFallback:
    """dom_for base implementation (fallback) handles unknown MlodyValue subtypes."""

    def test_fallback_returns_text_of_repr(self) -> None:
        """Unregistered MlodyValue subtype returns text(repr(value))."""
        from dataclasses import dataclass

        from mlody.resolver.values.base import MlodyValue

        @dataclass(frozen=True)
        class _CustomValue(MlodyValue):
            payload: str

        v = _CustomValue(payload="hello")
        rendered = _render(dom_for(v))
        # repr will contain _CustomValue and payload
        assert "_CustomValue" in rendered or "hello" in rendered


# ---------------------------------------------------------------------------
# Import isolation: render.py must not import engine/
# ---------------------------------------------------------------------------


class TestRenderModuleIsolation:
    """render.py must not import from mlody.resolver.engine (Wave 3c boundary)."""

    def test_render_has_no_engine_imports(self) -> None:
        import importlib
        import inspect

        render = importlib.import_module("mlody.resolver.render")
        src = inspect.getsource(render)
        # Docstrings may mention engine for documentation; import statements must not.
        assert "import mlody.resolver.engine" not in src
        assert "from mlody.resolver.engine" not in src
