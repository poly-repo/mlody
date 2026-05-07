"""DAG virtual value type and factory for mlody output port traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx

    from mlody.core.workspace import Workspace


@dataclass(frozen=True)
class MlodyDagType:
    """Type descriptor for a DAG virtual value.

    Python-only — not a Struct. Used as the ``type`` field of the virtual
    value wrapper so that ``show.py`` can dispatch on it via ``isinstance``.
    """

    kind: str = "type"
    type: str = "mlody-dag"
    name: str = "mlody-dag"


DAG_TYPE = MlodyDagType()


def make_dag_virtual_value(
    workspace: "Workspace",
    port_name: str,
    label: str,
) -> object:
    """Return a virtual value Struct whose materialiser returns the ancestor subgraph.

    The materialiser calls ``workspace.dag`` (lazily built, cached) then
    ``ancestors_subgraph(dag, port_name)``.  The result is a raw
    ``networkx.MultiDiGraph``; ``show.py`` detects it via
    ``isinstance(value_type, MlodyDagType)``.

    Args:
        workspace: Fully-loaded ``Workspace`` instance.
        port_name: Output port name used to filter the ancestor subgraph,
            and stored as ``name`` on the virtual value for title rendering.
        label: Dot-path label (e.g. ``":task.outputs.my_output.dag"``) stored
            on the virtual value for traceability.
    """
    from mlody.core.virtual_value import make_virtual_value  # noqa: PLC0415

    def _materializer(_v: object) -> "networkx.MultiDiGraph":
        from mlody.core.dag import ancestors_subgraph  # noqa: PLC0415

        return ancestors_subgraph(workspace.dag, port_name)

    return make_virtual_value(
        value_type=DAG_TYPE,
        label=label,
        materializer=_materializer,
        name=port_name,
    )
