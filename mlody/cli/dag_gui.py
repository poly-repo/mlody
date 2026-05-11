"""GUI rendering pipeline for DAG visualisation.

This module exposes a single public function, :func:`show_dag_gui`, which
accepts a ``networkx.MultiDiGraph`` produced by :func:`mlody.core.dag.build_dag`
(or :func:`mlody.core.dag.ancestors_subgraph`) and opens a blocking native
desktop window showing the graph as a directed node-link diagram.

Rendering contract
------------------
- Inputs: a :class:`networkx.MultiDiGraph` whose node data contains a ``"task"``
  key of type :class:`~mlody.core.dag.TaskNode` and whose edge data contains an
  ``"edge"`` key of type :class:`~mlody.core.dag.Edge`.
- The function does **not** return until the user closes the window (blocking;
  FR-006 / FR-007).
- ``matplotlib`` is imported lazily inside :func:`show_dag_gui`, not at module
  load time, so importing this module does not incur the matplotlib import cost
  and does not fail in headless environments (NFR-M-003).

Two-step build / display split
-------------------------------
The pipeline is split into :func:`_build_figure` (layout + drawing) and
``plt.show()`` (window event loop) so that a future interactive iteration can
replace ``plt.show()`` with ``plt.show(block=False)`` plus
``fig.canvas.mpl_connect(...)`` without modifying the layout or drawing code
(FR-009).
"""

from __future__ import annotations

import math

import networkx

# ---------------------------------------------------------------------------
# Colour palette — Catppuccin-inspired dark theme (D-4)
# ---------------------------------------------------------------------------

_BG_COLOUR = "#1e1e2e"
_NODE_FILL = "#313244"
_NODE_BORDER = "#89b4fa"
_TEXT_COLOUR = "#cdd6f4"
_EDGE_COLOUR = "#a6e3a1"
_TAIL_LABEL_COLOUR = "#f9e2af"
_HEAD_LABEL_COLOUR = "#fab387"

# ---------------------------------------------------------------------------
# Layout constants — fixed "max readable" node size; spacing derived from it
# so overlap is impossible.
# ---------------------------------------------------------------------------

#: Half-width of every node box in data units.
_NODE_HX: float = 0.55
#: Half-height of every node box in data units.
_NODE_HW: float = 0.20
#: Horizontal centre-to-centre distance between adjacent layers.
#: Must be > 2 * _NODE_HX to guarantee no horizontal overlap.
_LAYER_SEP: float = _NODE_HX * 2 + 0.7
#: Vertical centre-to-centre distance between sibling nodes in the same layer.
#: Must be > 2 * _NODE_HW to guarantee no vertical overlap.
_NODE_SEP: float = _NODE_HW * 2 + 0.30
#: Fontsize used for node labels (fixed — readable at the chosen node size).
_NODE_FONT: float = 9.0


# ---------------------------------------------------------------------------
# Private helpers — typed against matplotlib types after lazy import
# ---------------------------------------------------------------------------


def _hierarchical_layout(
    dag: networkx.MultiDiGraph,
) -> dict[str, tuple[float, float]]:
    """Compute node positions using a graphviz-style hierarchical algorithm.

    Steps:
    1. **Layer assignment** — assign each node to a topological generation.
    2. **Crossing minimisation** — run several passes of the barycenter
       heuristic: for each layer (left to right), sort its nodes by the
       average rank of their predecessors in the previous layer.
    3. **Coordinate assignment** — place layers at fixed horizontal intervals
       (_LAYER_SEP) and nodes within a layer at fixed vertical intervals
       (_NODE_SEP), centred on y = 0.

    The layout guarantees no overlap: _LAYER_SEP > 2 * _NODE_HX and
    _NODE_SEP > 2 * _NODE_HW by construction.

    Args:
        dag: A ``networkx.MultiDiGraph`` to lay out.

    Returns:
        Mapping of node ID to ``(x, y)`` in data coordinates.
    """
    generations = list(networkx.topological_generations(dag))
    # Start with a deterministic ordering within each layer.
    ordered: list[list[str]] = [sorted(layer) for layer in generations]

    # Barycenter crossing minimisation — a few forward passes suffice for
    # typical pipeline DAGs.
    for _ in range(4):
        for i in range(1, len(ordered)):
            prev_rank = {nid: j for j, nid in enumerate(ordered[i - 1])}
            ordered[i].sort(
                key=lambda nid: (  # noqa: B023
                    sum(prev_rank.get(p, 0) for p in dag.predecessors(nid))  # noqa: B023
                    / max(1, sum(1 for _ in dag.predecessors(nid)))  # noqa: B023
                )
            )

    pos: dict[str, tuple[float, float]] = {}
    for layer_idx, layer in enumerate(ordered):
        x = layer_idx * _LAYER_SEP
        n = len(layer)
        for node_idx, nid in enumerate(layer):
            y = (node_idx - (n - 1) / 2.0) * _NODE_SEP
            pos[nid] = (x, y)

    return pos


def _draw_nodes(
    ax: object,
    pos: dict[str, tuple[float, float]],
    dag: networkx.MultiDiGraph,
    *,
    node_hx: float = 0.12,
    node_hw: float = 0.06,
) -> dict[str, object]:
    """Draw each node as a rounded rectangle and return a mapping of node ID to patch.

    The node label is taken from ``dag.nodes[nid]["task"].name`` (the bare task
    name; FR-003).  The returned dict is reserved for future interactive layers
    that need to attach pick handlers to individual patches (FR-009).

    Args:
        ax:      A matplotlib ``Axes`` instance.
        pos:     Node-position mapping produced by ``networkx.multipartite_layout``.
        dag:     The graph whose nodes are to be drawn.
        node_hx: Half-width of each node box in data coordinates.
        node_hw: Half-height of each node box in data coordinates.

    Returns:
        Mapping of node ID to the :class:`matplotlib.patches.FancyBboxPatch`
        that represents it.
    """
    from matplotlib.patches import FancyBboxPatch  # type: ignore[import-untyped]

    pad = node_hw * 0.25

    patches: dict[str, object] = {}
    for nid, data in dag.nodes(data=True):
        x, y = pos[nid]
        node_meta = data.get("task") or data.get("value")
        task_name: str = node_meta.name if node_meta is not None else nid

        patch = FancyBboxPatch(
            (x - node_hx, y - node_hw),
            node_hx * 2,
            node_hw * 2,
            boxstyle=f"round,pad={pad}",
            facecolor=_NODE_FILL,
            edgecolor=_NODE_BORDER,
            linewidth=1.5,
            zorder=3,
        )
        ax.add_patch(patch)  # type: ignore[union-attr]
        ax.text(  # type: ignore[union-attr]
            x,
            y,
            task_name,
            color=_TEXT_COLOUR,
            ha="center",
            va="center",
            fontsize=_NODE_FONT,
            fontweight="bold",
            zorder=4,
        )
        patches[nid] = patch

    return patches


def _box_exit(
    cx: float,
    cy: float,
    tx: float,
    ty: float,
    hx: float,
    hw: float,
) -> tuple[float, float]:
    """Return the point where the segment (cx,cy)→(tx,ty) exits a box.

    The box is axis-aligned, centred on (cx, cy) with half-width ``hx`` and
    half-height ``hw``.  Used to clip arrow endpoints to node-box boundaries
    so arrowheads are visible and not hidden behind the node fill.

    Args:
        cx, cy: Centre of the source/destination box.
        tx, ty: The opposite endpoint of the edge (centre of the other node).
        hx:     Half-width of the box in data coordinates.
        hw:     Half-height of the box in data coordinates.

    Returns:
        The intersection point on the box edge closest to (tx, ty).
    """
    dx = tx - cx
    dy = ty - cy
    if dx == 0.0 and dy == 0.0:
        return cx, cy
    # Compute parametric t where the ray first hits each box face.
    t_x = hx / abs(dx) if dx != 0.0 else math.inf
    t_y = hw / abs(dy) if dy != 0.0 else math.inf
    t = min(t_x, t_y)
    return cx + dx * t, cy + dy * t


def _draw_edges(
    ax: object,
    pos: dict[str, tuple[float, float]],
    dag: networkx.MultiDiGraph,
) -> None:
    """Draw directed arrows for every edge with tail and head labels.

    For each edge ``(u, v, k, data)`` in the graph:
    - Draws a :class:`matplotlib.patches.FancyArrowPatch` using
      ``connectionstyle="arc3,rad=R"`` where ``R`` is derived from the edge
      index ``k`` so that parallel edges fan out symmetrically (D-3).
    - Arrow endpoints are clipped to the node-box boundary so arrowheads
      are visible and not hidden behind node fills.
    - Places a tail label (``data["edge"].src_port``) at ``t=0.18`` along the
      straight line from source to destination (FR-004).
    - Places a head label (``data["edge"].dst_path``) at ``t=0.82`` (FR-004).

    The two label colours differ (NFR-U-001): tail labels use ``_TAIL_LABEL_COLOUR``
    and head labels use ``_HEAD_LABEL_COLOUR``.

    Args:
        ax:  A matplotlib ``Axes`` instance.
        pos: Node-position mapping produced by ``_hierarchical_layout``.
        dag: The graph whose edges are to be drawn.
    """
    from matplotlib.patches import FancyArrowPatch  # type: ignore[import-untyped]

    for u, v, k, data in dag.edges(data=True, keys=True):  # type: ignore[misc]
        x_src, y_src = pos[u]
        x_dst, y_dst = pos[v]

        # Clip to box boundaries so the arrow starts/ends at the node edge.
        sx, sy = _box_exit(x_src, y_src, x_dst, y_dst, _NODE_HX, _NODE_HW)
        ex, ey = _box_exit(x_dst, y_dst, x_src, y_src, _NODE_HX, _NODE_HW)

        # Compute curvature for multi-edge fan-out (D-3).
        # k=0 → R=0.0 (straight), k=1 → +0.25, k=2 → -0.25, k=3 → +0.50, …
        if k == 0:
            rad = 0.0
        else:
            rad = 0.25 * ((-1) ** k) * math.ceil((k + 1) / 2)

        arrow = FancyArrowPatch(
            (sx, sy),
            (ex, ey),
            connectionstyle=f"arc3,rad={rad}",
            arrowstyle="-|>",
            color=_EDGE_COLOUR,
            linewidth=1.2,
            mutation_scale=14,
            zorder=2,
        )
        ax.add_patch(arrow)  # type: ignore[union-attr]

        # Label positions along the clipped arrow vector.
        edge_obj = data["edge"]

        # Perpendicular offset to lift labels off the arrow shaft.
        adx = ex - sx
        ady = ey - sy
        length = math.hypot(adx, ady) or 1.0
        perp_x = -ady / length * 0.06
        perp_y = adx / length * 0.06

        # Tail label at t=0.20 from the source box edge.
        t_tail = 0.20
        lx_tail = sx + t_tail * adx + perp_x
        ly_tail = sy + t_tail * ady + perp_y
        ax.annotate(  # type: ignore[union-attr]
            edge_obj.src_port,
            (lx_tail, ly_tail),
            color=_TAIL_LABEL_COLOUR,
            fontsize=7,
            ha="center",
            va="center",
            zorder=5,
        )

        # Head label at t=0.80, near the arrowhead.
        t_head = 0.80
        lx_head = sx + t_head * adx + perp_x
        ly_head = sy + t_head * ady + perp_y
        ax.annotate(  # type: ignore[union-attr]
            edge_obj.dst_path,
            (lx_head, ly_head),
            color=_HEAD_LABEL_COLOUR,
            fontsize=7,
            ha="center",
            va="center",
            zorder=5,
        )


def _build_figure(
    dag: networkx.MultiDiGraph,
    title: str,
) -> tuple[object, object]:
    """Build a matplotlib figure showing ``dag`` as a directed node-link diagram.

    The original ``dag`` object is not mutated — a copy is made for the layout
    step so that the ``"layer"`` attribute added during layout does not leak back
    to the caller's graph (D-2).

    Steps (D-2, D-5, D-8):
    1. Copy ``dag`` to ``layout_dag`` and annotate each node with a ``"layer"``
       attribute using :func:`networkx.topological_generations`.
    2. Compute positions via :func:`networkx.multipartite_layout`.
    3. Derive figure size from the data bounding box, capped to a reasonable
       maximum so the window fits on screen.
    4. Apply the dark background palette (D-4).
    5. Set title on both axes and window title bar (D-8).
    6. Delegate drawing to :func:`_draw_nodes` and :func:`_draw_edges`.
    7. Set explicit axis limits with padding so node boxes and labels are never
       clipped, then turn off decorations.

    Args:
        dag:   Graph to render.  Node data must contain ``"task"``; edge data
               must contain ``"edge"``.
        title: Axes title string and OS window title bar string.

    Returns:
        A ``(fig, ax)`` tuple of matplotlib Figure and Axes objects.
    """
    import matplotlib.pyplot as plt  # type: ignore[import-untyped]

    pos = _hierarchical_layout(dag)

    # Bounding box of all node positions.
    xs = [p[0] for p in pos.values()] if pos else [0.0]
    ys = [p[1] for p in pos.values()] if pos else [0.0]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    # Padding: node half-size plus room for edge labels.
    x_pad = _NODE_HX * 2.8
    y_pad = _NODE_HW * 4.0

    # Figure size: 1 data unit ≈ 1 inch, capped to fit a typical screen.
    fig_w = min(max(8.0, (x_max - x_min) + 2 * x_pad), 28.0)
    fig_h = min(max(5.0, (y_max - y_min) + 2 * y_pad), 20.0)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor(_BG_COLOUR)
    ax.set_facecolor(_BG_COLOUR)
    ax.set_title(title, color=_TEXT_COLOUR, fontsize=11, pad=12)

    try:
        fig.canvas.manager.set_window_title(title)  # type: ignore[union-attr]
    except AttributeError:
        pass

    _draw_nodes(ax, pos, dag, node_hx=_NODE_HX, node_hw=_NODE_HW)
    _draw_edges(ax, pos, dag)

    # Explicit limits — tight_layout() ignores FancyBboxPatch extents.
    ax.set_xlim(x_min - x_pad, x_max + x_pad)
    ax.set_ylim(y_min - y_pad, y_max + y_pad)
    ax.set_axis_off()
    fig.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.02)
    return fig, ax


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def show_dag_gui(
    dag: networkx.MultiDiGraph,
    title: str,
) -> None:
    """Open a blocking native window showing the DAG as a node-link diagram.

    Renders every node in ``dag`` as a labelled rounded rectangle and every
    edge as a directed arrow with two positional labels: ``src_port`` near the
    tail and ``dst_path`` near the arrowhead.

    This function does not return until the user closes the window (blocking,
    FR-006 / FR-007).

    The rendering pipeline is intentionally split into a figure-building step
    (_build_figure) and a display step (plt.show()).  Future interactive
    iterations can replace the display step with an event-loop that attaches
    pick/scroll handlers to the figure returned by _build_figure without
    modifying the layout or drawing code (FR-009).

    Args:
        dag:   A ``networkx.MultiDiGraph`` produced by ``build_dag`` or
               ``ancestors_subgraph``.  Node data must contain a ``"task"``
               key of type ``TaskNode``; edge data must contain an ``"edge"``
               key of type ``Edge``.
        title: Window title bar string and axes title (e.g. ``"Workspace DAG"``
               or ``"DAG \u2014 ancestors of 'model_checkpoint'"``).
    """
    import matplotlib  # type: ignore[import-untyped]

    # Import pyplot with the safe non-interactive Agg backend first, then
    # switch to the first interactive backend whose toolkit is available.
    # switch_backend() actually loads the backend module and raises on failure,
    # unlike matplotlib.use() which only stores a string.
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore[import-untyped]

    _BACKENDS = ("QtAgg", "Qt5Agg", "TkAgg", "GTK4Agg", "GTK3Agg", "WXAgg", "MacOSX")
    for _backend in _BACKENDS:
        try:
            plt.switch_backend(_backend)
            break
        except Exception:  # noqa: BLE001
            continue
    else:
        raise RuntimeError(
            "No interactive matplotlib backend is available. "
            "Install one of: python3-tk, PyQt5/PyQt6/PySide6, wxPython."
        )

    _build_figure(dag, title)
    plt.show()
