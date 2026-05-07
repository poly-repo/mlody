"""dag subcommand — display the workspace task dependency graph."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import networkx
from rich.console import Console

from mlody.cli.dag_gui import show_dag_gui
from mlody.cli.dag_render import render_dag_table, resolve_dag_selection
from mlody.cli.main import cli
from mlody.core.workspace import Workspace, WorkspaceLoadError

_console = Console()


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

    dag = workspace.dag

    if label is None:
        display_graph = dag
        title = "Workspace DAG"
    else:
        selection = resolve_dag_selection(dag, label)
        display_graph = selection.graph
        if len(display_graph.nodes) == 0:
            msg = f"Error: no task produces value '{selection.resolved_label}'"
            if selection.resolved_label != label:
                msg += f" (resolved from '{label}')"
            click.echo(click.style(msg, fg="red"), err=True)
            if selection.suggestion_text:
                click.echo(
                    click.style(selection.suggestion_text, fg="yellow"),
                    err=True,
                )
            sys.exit(1)
        title = f"DAG \u2014 ancestors of '{label}'"

    # Topological order so dependencies always appear before dependents.
    try:
        render_dag_table(display_graph, title, console=_console)
    except networkx.NetworkXUnfeasible:
        click.echo(click.style("Error: cycle detected in task graph", fg="red"), err=True)
        sys.exit(1)

    if gui:
        show_dag_gui(display_graph, title)
