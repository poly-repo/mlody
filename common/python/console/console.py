from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich.markdown import Markdown
from rich.tree import Tree


class RichDomNode(Protocol):
    def render(self, ctx: "RenderContext") -> RenderableType: ...


@dataclass
class RenderContext:
    console: Console


@dataclass
class Fragment:
    children: list[RichDomNode]

    def render(self, ctx: RenderContext) -> RenderableType:
        return Columns([child.render(ctx) for child in self.children], equal=False)


@dataclass
class TextNode:
    value: str
    style: str | None = None

    def render(self, ctx: RenderContext) -> RenderableType:
        return Text(self.value, style=self.style)


@dataclass
class MarkdownNode:
    value: str

    def render(self, ctx: RenderContext) -> RenderableType:
        return Markdown(self.value)


@dataclass
class PanelNode:
    child: RichDomNode
    title: str | None = None
    border_style: str | None = None

    def render(self, ctx: RenderContext) -> RenderableType:
        kwargs = {"title": self.title}
        if self.border_style is not None:
            kwargs["border_style"] = self.border_style
        return Panel(self.child.render(ctx), **kwargs)


@dataclass
class TableNode:
    columns: list[str]
    rows: list[list[RichDomNode | str]]
    title: str | None = None

    def render(self, ctx: RenderContext) -> RenderableType:
        table = Table(title=self.title)

        for column in self.columns:
            table.add_column(column)

        for row in self.rows:
            table.add_row(
                *[
                    cell.render(ctx) if hasattr(cell, "render") else str(cell)
                    for cell in row
                ]
            )

        return table


@dataclass
class TreeNode:
    label: RichDomNode | str
    children: list[TreeNode | RichDomNode | str] = field(default_factory=list)

    def render(self, ctx: RenderContext) -> RenderableType:
        label = (
            self.label.render(ctx) if hasattr(self.label, "render") else str(self.label)
        )
        tree = Tree(label)

        def add(parent: Tree, child: TreeNode | RichDomNode | str) -> None:
            if isinstance(child, TreeNode):
                rendered_label = (
                    child.label.render(ctx)
                    if hasattr(child.label, "render")
                    else str(child.label)
                )
                branch = parent.add(rendered_label)
                for grandchild in child.children:
                    add(branch, grandchild)
            elif hasattr(child, "render"):
                parent.add(child.render(ctx))
            else:
                parent.add(str(child))

        for child in self.children:
            add(tree, child)

        return tree


# -----------------------------------------------------------------------------
# Factory functions
# -----------------------------------------------------------------------------


@dataclass
class SyntaxNode:
    value: str
    language: str = "python"
    theme: str = "monokai"

    def render(self, ctx: RenderContext) -> RenderableType:
        return Syntax(self.value, self.language, theme=self.theme, word_wrap=True)


@dataclass
class StackNode:
    children: list[RichDomNode]

    def render(self, ctx: RenderContext) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        return Group(*[child.render(ctx) for child in self.children])


def text(value: str, style: str | None = None) -> TextNode:
    return TextNode(value, style)


def syntax(value: str, language: str = "python", theme: str = "monokai") -> SyntaxNode:
    return SyntaxNode(value, language, theme)


def md(value: str) -> MarkdownNode:
    return MarkdownNode(value)


def panel(
    child: RichDomNode,
    *,
    title: str | None = None,
    border_style: str | None = None,
) -> PanelNode:
    return PanelNode(child, title=title, border_style=border_style)


def table(
    columns: list[str],
    rows: list[list[RichDomNode | str]],
    *,
    title: str | None = None,
) -> TableNode:
    return TableNode(columns, rows, title=title)


def tree(
    label: RichDomNode | str,
    children: Iterable[TreeNode | RichDomNode | str] = (),
) -> TreeNode:
    return TreeNode(label, list(children))


def stack(*children: RichDomNode) -> StackNode:
    return StackNode(list(children))


def fragment(*children: RichDomNode) -> Fragment:
    return Fragment(list(children))


# -----------------------------------------------------------------------------
# Executor
# -----------------------------------------------------------------------------


class RichDomExecutor:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def render(self, node: RichDomNode) -> None:
        ctx = RenderContext(console=self.console)
        self.console.print(node.render(ctx))
