"""dag subcommand — display the workspace task dependency graph."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
import networkx
from rich.console import Console
from rich.table import Table

from mlody.cli.dag_gui import show_dag_gui
from mlody.cli.main import cli
from mlody.core.dag import Edge, TaskNode, ancestors_subgraph, build_dag
from mlody.core.targets import parse_target
from mlody.core.workspace import Workspace, WorkspaceLoadError

_logger = logging.getLogger(__name__)
_console = Console()


def _short_type_name(value: object) -> str:
    """Return a concise type label for a value-like object."""
    t = getattr(value, "type", None)
    if t is None:
        return "?"
    # value.type is normally a type struct with a .name field
    t_name = getattr(t, "name", None)
    if isinstance(t_name, str) and t_name:
        return t_name
    if isinstance(t, str) and t:
        return t
    return "?"


def _format_value_list(values: object) -> str:
    """Format ports/config entries as `name:type` with short type names."""
    if not isinstance(values, list) or not values:
        return "—"

    rendered: list[str] = []
    for v in values:
        name = getattr(v, "name", None)
        if not isinstance(name, str) or not name:
            name = str(v)
        rendered.append(f"{name}:{_short_type_name(v)}")
    return ", ".join(rendered)


def _format_action_cell(action_obj: object, fallback_name: str) -> str:
    """Format action name plus AIn/AOut/ACfg summaries."""
    if action_obj is None:
        return fallback_name

    name = getattr(action_obj, "name", None)
    if not isinstance(name, str) or not name:
        name = fallback_name

    a_inputs = _format_value_list(getattr(action_obj, "inputs", []))
    a_outputs = _format_value_list(getattr(action_obj, "outputs", []))
    a_config = _format_value_list(getattr(action_obj, "config", []))
    return f"{name}\nAIn:  {a_inputs}\nAOut: {a_outputs}\nACfg: {a_config}"


def _subgraph_for_label(
    dag: networkx.MultiDiGraph, label: str
) -> tuple[networkx.MultiDiGraph, str]:
    """Resolve a mlody label to an ancestor subgraph and the resolved name.

    Returns ``(subgraph, resolved)`` where ``resolved`` is the port name or
    node ID that was actually queried (useful for error messages).

    Handles three forms:
    - ``//pkg:task.outputs.port`` — ancestors of the named output port.
    - ``//pkg:task`` or ``:task`` — ancestors of the task node itself.
    - Bare port name — passed directly to ``ancestors_subgraph`` (legacy).
    """
    try:
        addr = parse_target(label)
    except ValueError:
        return ancestors_subgraph(dag, label), label

    # Output port reference: field_path == ('outputs', port_name)
    if len(addr.field_path) == 2 and addr.field_path[0] == "outputs":
        port_name = addr.field_path[1]
        return ancestors_subgraph(dag, port_name), port_name

    # Task reference: no field_path → ancestors of the task node itself
    if not addr.field_path:
        node_id: str | None = None
        if addr.package_path is not None:
            candidate = f"task/{addr.package_path}:{addr.target_name}"
            if candidate in dag.nodes:
                node_id = candidate
        else:
            # :name shorthand — match by bare task name
            for nid, data in dag.nodes(data=True):
                task_node: TaskNode = data["task"]
                if task_node.name == addr.target_name:
                    node_id = nid
                    break
        if node_id is not None:
            all_nodes = networkx.ancestors(dag, node_id) | {node_id}
            result: networkx.MultiDiGraph = dag.subgraph(all_nodes).copy()
            return result, node_id

    return networkx.MultiDiGraph(), label


def _suggest_label_fix(
    dag: networkx.MultiDiGraph, label: str
) -> str | None:
    """Return a hint if the label looks like it's missing an outputs/inputs prefix.

    Fires when field_path has exactly one segment that matches an output or
    input port on the referenced task — e.g. ``:finetune_text.text_model``
    instead of ``:finetune_text.outputs.text_model``.
    """
    try:
        addr = parse_target(label)
    except ValueError:
        return None

    if len(addr.field_path) != 1:
        return None

    port_name = addr.field_path[0]

    # Locate the task node
    node_id: str | None = None
    if addr.package_path is not None:
        candidate = f"task/{addr.package_path}:{addr.target_name}"
        if candidate in dag.nodes:
            node_id = candidate
    else:
        for nid, data in dag.nodes(data=True):
            tn: TaskNode = data["task"]
            if tn.name == addr.target_name:
                node_id = nid
                break

    if node_id is None:
        return None

    task_node: TaskNode = dag.nodes[node_id]["task"]

    # Build the canonical label prefix (everything before the field_path segment)
    if addr.package_path is not None:
        root_prefix = f"@{addr.root}//" if addr.root else "//"
        base = f"{root_prefix}{addr.package_path}:{addr.target_name}"
    else:
        base = f":{addr.target_name}"

    suggestions: list[str] = []
    if port_name in task_node.output_ports:
        suggestions.append(f"'{base}.outputs.{port_name}'")
    if port_name in task_node.input_ports:
        suggestions.append(f"'{base}.inputs.{port_name}'")

    if suggestions:
        return "Did you mean: " + " or ".join(suggestions) + "?"
    return None


@cli.command("dag")
@click.argument("label", required=False, default=None)
@click.option(
    "--gui",
    is_flag=True,
    default=False,
    help="Open a GUI window showing the DAG diagram (blocking until closed).",
)
@click.pass_context
def dag_cmd(ctx: click.Context, label: str | None, gui: bool) -> None:
    """Display the workspace task dependency graph.

    When VALUE is omitted all tasks are shown in topological order
    (full-graph path, unchanged behaviour).

    When VALUE is provided, only the ancestor subgraph — the minimal set
    of tasks that transitively contribute to that value — is rendered.
    VALUE is a mlody label in one of two forms:

    \b
      //pkg:task.outputs.port  — ancestors of a specific output port
      //pkg:task               — ancestors of a task (all its outputs)

    Shorthand ``:task`` and ``:task.outputs.port`` are also accepted.

    If VALUE does not match any task or output port the command prints a
    red error to stderr and exits with code 1.

    Pass --gui to open a native desktop window showing the same graph as a
    directed node-link diagram.  The window is blocking: the command does not
    return to the shell prompt until the window is closed.  The Rich table is
    always printed first, before the window opens.
    """
    monorepo_root: Path = ctx.obj["monorepo_root"]
    workspace_root: Path = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    verbose: bool = ctx.obj.get("verbose", False)
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    extra_roots: dict[str, str] | None = None
    lazy_roots: dict[str, str] | None = None
    if workspace_root != monorepo_root:
        workspace_rel = str(workspace_root.relative_to(monorepo_root))
        extra_roots = {"workspace": workspace_rel}
        if (monorepo_root / "mlody").is_dir():
            lazy_roots = {"mlody": "mlody"}

    workspace = Workspace(
        monorepo_root=monorepo_root,
        roots_file=roots,
        full_workspace=full_workspace,
        extra_roots=extra_roots,
        lazy_roots=lazy_roots,
    )
    try:
        workspace.load(verbose=verbose)
    except WorkspaceLoadError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        sys.exit(1)

    dag = build_dag(workspace)

    if label is None:
        display_graph = dag
        title = "Workspace DAG"
    else:
        display_graph, resolved = _subgraph_for_label(dag, label)
        if len(display_graph.nodes) == 0:
            msg = f"Error: no task produces value '{resolved}'"
            if resolved != label:
                msg += f" (resolved from '{label}')"
            click.echo(click.style(msg, fg="red"), err=True)
            hint = _suggest_label_fix(dag, label)
            if hint:
                click.echo(click.style(hint, fg="yellow"), err=True)
            sys.exit(1)
        title = f"DAG \u2014 ancestors of '{label}'"

    # Topological order so dependencies always appear before dependents.
    try:
        order = list(networkx.topological_sort(display_graph))
    except networkx.NetworkXUnfeasible:
        click.echo(click.style("Error: cycle detected in task graph", fg="red"), err=True)
        sys.exit(1)

    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Task", style="cyan", no_wrap=True, ratio=4)
    table.add_column("Action", style="magenta", no_wrap=False, ratio=2)
    table.add_column("Dependencies", style="white", ratio=5)

    for node_id in order:
        task_node = display_graph.nodes[node_id]["task"]
        task_struct = display_graph.nodes[node_id]["task_struct"]
        deps: list[str] = []
        for src_id, _, data in display_graph.in_edges(node_id, data=True):
            edge: Edge = data["edge"]
            deps.append(f"{src_id}\n  {edge.src_port} → {edge.dst_path}")
        inputs_str = _format_value_list(getattr(task_struct, "inputs", []))
        outputs_str = _format_value_list(getattr(task_struct, "outputs", []))
        config_str = _format_value_list(getattr(task_struct, "config", []))
        task_cell = f"{node_id}\nIn:  {inputs_str}\nOut: {outputs_str}\nCfg: {config_str}"
        table.add_row(
            task_cell,
            _format_action_cell(getattr(task_struct, "action", None), task_node.action),
            "\n\n".join(deps) if deps else "—",
        )

    _console.print(table)

    if gui:
        show_dag_gui(display_graph, title)
