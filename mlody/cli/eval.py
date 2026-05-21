"""eval subcommand — load external .mlody files and open an interactive shell."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from mlody.cli.main import cli
from mlody.cli.shell import _build_repl_namespace, _get_history_path, _launch_repl


@cli.command(name="eval")
@click.argument("files", nargs=-1, type=click.Path(path_type=Path, dir_okay=False))
@click.pass_context
def eval_cmd(ctx: click.Context, files: tuple[Path, ...]) -> None:
    """Inject .mlody files as monorepo-root modules and open an interactive shell.

    Each FILE is loaded as if it lived at the monorepo root, so a file named
    foo.mlody makes //foo:<name> labels resolvable in the shell.

    Repeatable: mlody eval a.mlody b.mlody
    """
    monorepo_root: Path = ctx.obj["monorepo_root"]
    workspace_root: Path = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    bwd = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    base = Path(bwd) if bwd else Path.cwd()
    eval_files: list[Path] = []
    for f in files:
        p = f if f.is_absolute() else (base / f).resolve()
        if not p.exists():
            click.echo(f"Error: eval file not found: {p}", err=True)
            sys.exit(1)
        eval_files.append(p)

    from mlody.resolver import resolve_workspace

    workspace_obj, _sha = resolve_workspace(
        "//mlody:_shell_init",
        monorepo_root=monorepo_root,
        workspace_root=workspace_root,
        roots_file=roots,
        full_workspace=full_workspace,
        eval_files=tuple(eval_files),
    )
    history_file = _get_history_path()
    namespace = _build_repl_namespace(workspace_obj, monorepo_root, workspace_root, full_workspace)
    _launch_repl(namespace, history_file)
