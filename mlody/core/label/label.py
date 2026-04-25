"""Frozen dataclasses representing a parsed mlody label."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntitySpec:
    """The entity component of a label (path, wildcard flag, optional name).

    ``field_path`` carries the dot-separated segments that follow the entity
    name inside the colon section of a label.  For example, in
    ``@lexica//diamond:pretrain.outputs.backbone_weights`` the parser sets
    ``name="pretrain"`` and ``field_path=("outputs", "backbone_weights")``.

    ``None`` means no dot appeared after the entity name (bare ``:pretrain``).
    An empty tuple is never produced by the parser.
    """

    root: str | None
    path: str | None
    wildcard: bool
    name: str | None
    # None = no dot suffix after entity name; non-empty tuple = traversal path.
    field_path: tuple[str, ...] | None


@dataclass(frozen=True)
class Label:
    """A fully-parsed mlody label.

    None on any field means the corresponding component was absent from
    the original label string, not that it was empty.  The distinction
    matters: workspace=None means "current workspace", workspace="" is
    invalid and should never be produced by the parser.
    """

    workspace: str | None
    workspace_query: str | None
    entity: EntitySpec | None
    entity_query: str | None
    # None means no "'" separator was present; an empty tuple is not used.
    attribute_path: tuple[str, ...] | None
    attribute_query: str | None

    def format_inner(self) -> str:
        """Re-serialise the label without the workspace/committoid prefix."""
        if self.entity is None and self.attribute_path is None:
            return ""
        if self.entity is None:
            assert self.attribute_path is not None
            attr_str = ".".join(self.attribute_path)
            if self.attribute_query:
                attr_str += f"[{self.attribute_query}]"
            return f"'{attr_str}"
        parts: list[str] = []
        if self.entity.root is not None:
            parts.append(f"@{self.entity.root}")
        path = self.entity.path or ""
        if self.entity.wildcard:
            parts.append(f"//{path}/...")
        elif path:
            parts.append(f"//{path}")
        if self.entity.name is not None:
            parts.append(f":{self.entity.name}")
            if self.entity.field_path:
                parts.append("." + ".".join(self.entity.field_path))
            if self.entity_query is not None:
                parts.append(f"[{self.entity_query}]")
        if self.attribute_path:
            parts.append("'" + ".".join(self.attribute_path))
            if self.attribute_query is not None:
                parts.append(f"[{self.attribute_query}]")
        return "".join(parts)
