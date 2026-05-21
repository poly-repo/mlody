"""shell subcommand — interactive ptpython REPL with pre-populated mlody namespace."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from mlody.cli.main import cli
from mlody.cli.show import show_fn
from mlody.core.workspace import Workspace

if TYPE_CHECKING:
    pass


def _get_history_path() -> Path:
    """Return the REPL history file path, creating the parent directory if needed.

    Uses the XDG data directory convention (~/.local/share/mlody/) to keep
    history out of the home directory root. If the directory cannot be created
    (e.g., read-only filesystem), the failure is swallowed — REPL startup is
    never blocked by a missing history file.
    """
    history_file = Path.home() / ".local" / "share" / "mlody" / "repl_history"
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return history_file


def _build_repl_namespace(
    workspace: Workspace,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    full_workspace: bool = False,
) -> dict[str, object]:
    """Construct the REPL namespace exposed to the user.

    Exposes `show` as a callable that accepts a raw label string and resolves
    it via show_fn (which handles committoid-qualified labels). `workspace` is
    also exposed directly for advanced inspection of the cwd workspace.
    """
    def _show(*labels: str) -> object | list[object]:
        results = [
            show_fn(
                label,
                monorepo_root=monorepo_root,
                workspace_root=workspace_root,
                full_workspace=full_workspace,
            )
            for label in labels
        ]
        if len(results) == 1:
            return results[0]
        return results

    return {
        "show": _show,
        "workspace": workspace,
    }


def _launch_repl(namespace: dict[str, object], history_file: Path) -> None:
    """Launch the ptpython REPL with the given namespace and history file.

    Isolated as a separate function to act as a test seam — callers mock this
    to verify namespace construction without starting an interactive process.
    """
    from ptpython.repl import embed  # deferred import — ptpython is heavyweight

    embed(
        locals=namespace,  # type: ignore[arg-type]  # dict[str, object] ≈ dict[str, Any]
        history_filename=str(history_file),
        title="mlody shell",
    )


@cli.command()
@click.option(
    "--eval",
    "eval_files",
    type=click.Path(path_type=Path, dir_okay=False),
    multiple=True,
    help=(
        "Inject a .mlody file as a monorepo-root module before starting the shell. "
        "Repeatable: --eval a.mlody --eval b.mlody"
    ),
)
@click.pass_context
def shell(ctx: click.Context, eval_files: tuple[Path, ...]) -> None:
    """Launch an interactive Python REPL with the mlody namespace pre-loaded.

    Available in the REPL:
      show("@root//pkg:target")  — resolve and return a pipeline value
      workspace                  — the loaded Workspace instance
    """
    # Support legacy test injection of a pre-built workspace via ctx.obj
    if "workspace" in ctx.obj:
        workspace: Workspace = ctx.obj["workspace"]
        monorepo_root: Path = ctx.obj.get("monorepo_root", Path.cwd())
        workspace_root: Path | None = ctx.obj.get("workspace_root")
        full_workspace: bool = ctx.obj.get("full_workspace", False)
        history_file = _get_history_path()
        namespace = _build_repl_namespace(workspace, monorepo_root, workspace_root, full_workspace)
        _launch_repl(namespace, history_file)
        return

    monorepo_root = ctx.obj["monorepo_root"]
    workspace_root = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    bwd = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    base = Path(bwd) if bwd else Path.cwd()
    resolved_eval_files: list[Path] = []
    for ef in eval_files:
        p = ef if ef.is_absolute() else (base / ef).resolve()
        if not p.exists():
            click.echo(f"Error: --eval file not found: {p}", err=True)
            sys.exit(1)
        resolved_eval_files.append(p)

    from mlody.resolver import resolve_workspace

    workspace_obj, _sha = resolve_workspace(
        "//mlody:_shell_init",
        monorepo_root=monorepo_root,
        workspace_root=workspace_root,
        roots_file=roots,
        full_workspace=full_workspace,
        eval_files=tuple(resolved_eval_files),
    )
    history_file = _get_history_path()
    namespace = _build_repl_namespace(workspace_obj, monorepo_root, workspace_root, full_workspace)
    prelude_path = monorepo_root / "mlody" / "shell" / "prelude.mlody"
    if prelude_path.exists():
        workspace_obj.registry_view.eval_file(prelude_path)
        namespace.update(workspace_obj.registry_view.host_module_globals(prelude_path))
    for ef in resolved_eval_files:
        virtual_path = monorepo_root / ef.name
        namespace.update(workspace_obj.registry_view.host_module_globals(virtual_path))
    _launch_repl(namespace, history_file)
