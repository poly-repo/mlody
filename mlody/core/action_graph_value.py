"""Action-graph virtual value type and factory for ``.agraph`` traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx

    from mlody.core.workspace import Workspace


@dataclass(frozen=True)
class MlodyActionGraphType:
    """Type descriptor for an action-graph virtual value."""

    kind: str = "type"
    type: str = "mlody-action-graph"
    name: str = "mlody-action-graph"


ACTION_GRAPH_TYPE = MlodyActionGraphType()


def make_action_graph_virtual_value(
    workspace: "Workspace",
    port_name: str,
    label: str,
) -> object:
    """Return a virtual value Struct whose materialiser returns an action graph."""
    from mlody.core.action_graph import (  # noqa: PLC0415
        build_action_graph,
        selection_for_label,
    )
    from mlody.core.virtual_value import make_virtual_value  # noqa: PLC0415

    def _materializer(_v: object) -> "networkx.DiGraph":
        selection = selection_for_label(workspace, label.removesuffix(".agraph"))
        return build_action_graph(selection)

    return make_virtual_value(
        value_type=ACTION_GRAPH_TYPE,
        label=label,
        materializer=_materializer,
        name=port_name,
    )
