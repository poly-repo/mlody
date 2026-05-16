"""Rendering functions for MlodyValue types.

``dom_for`` is a singledispatch function that accepts any ``MlodyValue`` and
returns a ``RichDomNode`` for terminal display.  All renderers are registered
in this module — value modules do NOT self-register (avoids circular imports).

render.py imports from ``mlody.resolver.values.*`` but NOT from
``mlody.resolver.engine`` — the rendering path is independent of traversal.
"""

from __future__ import annotations

from functools import singledispatch

from rich.pretty import pretty_repr

from common.python.console import (
    RichDomNode,
    SyntaxNode,
    text,
    panel,
    tree,
    table,
    stack,
)
from mlody.core.type_display import format_type_label
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.registry_backed import (
    MlodyActionValue,
    MlodyTaskValue,
    MlodyUserValue,
    MlodyValueValue,
)
from mlody.resolver.values.structural import (
    MlodyFolderValue,
    MlodySourceRangeValue,
    MlodySourceValue,
    MlodyUnresolvedValue,
    MlodyVectorValue,
    MlodyWorkspaceValue,
)


# ---------------------------------------------------------------------------
# Private rendering helpers (mirrors label_value.py originals)
# ---------------------------------------------------------------------------


def _fmt_type(t: object) -> str:
    return format_type_label(t)


def _fmt_location(loc: object) -> str:
    if loc is None:
        return "-"
    return str(getattr(loc, "type", "-"))


def _fmt_default(v: object) -> str:
    return "-" if v is None else str(v)


def _value_rows(container: object) -> list[list[RichDomNode]]:
    """Return table rows for a dict of value-structs (inputs/outputs/config)."""
    if not isinstance(container, dict):
        return []
    rows: list[list[RichDomNode]] = []
    for k, v in container.items():
        rows.append([
            text(str(getattr(v, "name", k))),
            text(_fmt_type(getattr(v, "type", None))),
            text(_fmt_location(getattr(v, "location", None))),
            text(_fmt_default(getattr(v, "default", None))),
        ])
    return rows


def _to_display_dict(obj: object) -> object:
    """Recursively convert a Starlark Struct to a plain Python dict for display.

    Replaces callables (validator functions, etc.) with a '<function>' placeholder
    so the result is safely passable to pretty_repr for indented rendering.
    """
    if hasattr(obj, "as_mapping"):
        return {k: _to_display_dict(v) for k, v in obj.as_mapping().items()}
    if isinstance(obj, dict):
        return {k: _to_display_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_to_display_dict(v) for v in obj)
    if callable(obj) and not isinstance(obj, type):
        return "<function>"
    return obj


def _entity_io_panel(title: str, struct: object) -> RichDomNode:
    """Render a task or action struct as a Rich panel with I/O tables.

    Shared by MlodyTaskValue and MlodyActionValue — the two callers differ only
    in the title prefix string ("task: ..." vs "action: ...").
    """
    io_cols = ["name", "type", "source", "default"]
    cfg_cols = ["name", "type", "source", "value"]

    nodes: list[RichDomNode] = []

    input_rows = _value_rows(getattr(struct, "inputs", None))
    if input_rows:
        nodes.append(table(io_cols, input_rows, title="inputs"))

    output_rows = _value_rows(getattr(struct, "outputs", None))
    if output_rows:
        nodes.append(table(io_cols, output_rows, title="outputs"))

    config_rows = _value_rows(getattr(struct, "config", None))
    if config_rows:
        nodes.append(table(cfg_cols, config_rows, title="config"))

    return panel(stack(*nodes) if nodes else text("(empty)"), title=title)


# ---------------------------------------------------------------------------
# dom_for — singledispatch renderer
# ---------------------------------------------------------------------------


@singledispatch
def dom_for(value: MlodyValue) -> RichDomNode:
    """Return a RichDomNode for rendering *value* to the terminal.

    The base (fallback) implementation returns a text node containing the repr
    of the value, handling any unregistered MlodyValue subtype gracefully.
    """
    return text(repr(value))


@dom_for.register(MlodyWorkspaceValue)
def _(v: MlodyWorkspaceValue) -> RichDomNode:
    name = v.name or "(cwd)"
    return panel(
        text(f"root: {v.root}"),
        title=f"workspace: {name}",
        border_style="blue",
    )


@dom_for.register(MlodyFolderValue)
def _(v: MlodyFolderValue) -> RichDomNode:
    child_nodes = [text(c) for c in v.children] if v.children else [text("(empty)")]
    return tree(f"folder: {v.path}", child_nodes)


@dom_for.register(MlodySourceValue)
def _(v: MlodySourceValue) -> RichDomNode:
    title = f"source: {v.path}.mlody"
    if v.abs_path is not None:
        try:
            content = v.abs_path.read_text()
            return panel(SyntaxNode(content, language="python"), title=title, border_style="green")
        except Exception:
            pass
    return panel(text(v.path + ".mlody"), title=title, border_style="green")


@dom_for.register(MlodyTaskValue)
def _(v: MlodyTaskValue) -> RichDomNode:
    name = getattr(v.struct, "name", "?")
    return _entity_io_panel(f"task: {name}", v.struct)


@dom_for.register(MlodyActionValue)
def _(v: MlodyActionValue) -> RichDomNode:
    name = getattr(v.struct, "name", "?")
    return _entity_io_panel(f"action: {name}", v.struct)


@dom_for.register(MlodyUserValue)
def _(v: MlodyUserValue) -> RichDomNode:
    s = v.struct
    name = getattr(s, "name", "?")
    description = getattr(s, "description", "")
    groups = getattr(s, "groups", []) or []
    group_text = ", ".join(str(group) for group in groups) if groups else "(none)"

    return panel(
        table(
            ["field", "value"],
            [
                ["description", str(description)],
                ["groups", group_text],
            ],
        ),
        title=f"user: {name}",
    )


@dom_for.register(MlodyValueValue)
def _(v: MlodyValueValue) -> RichDomNode:
    content = pretty_repr(_to_display_dict(v.struct), max_width=88)
    return panel(SyntaxNode(content, language="python"), title="value")


@dom_for.register(MlodyUnresolvedValue)
def _(v: MlodyUnresolvedValue) -> RichDomNode:
    return panel(
        text(v.reason),
        title=f"unresolved: {v.label!r}",
        border_style="red",
    )


@dom_for.register(MlodySourceRangeValue)
def _(v: MlodySourceRangeValue) -> RichDomNode:
    line_suffix = (
        str(v.start_line)
        if v.start_line == v.end_line
        else f"{v.start_line}-{v.end_line}"
    )
    header = f"# {v.filepath}:{line_suffix}"
    separator = "#"
    try:
        lines = v.abs_path.read_text().splitlines()
        snippet = "\n".join(lines[v.start_line - 1 : v.end_line])
        content = f"{header}\n{separator}\n{snippet}"
    except Exception:
        content = f"{header}\n{separator}\n(could not read {v.abs_path})"
    return SyntaxNode(content, language="python")


@dom_for.register(_RawAttrValue)
def _(v: _RawAttrValue) -> RichDomNode:
    return text(str(v.value))
