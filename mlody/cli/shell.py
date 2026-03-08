"""shell subcommand — interactive ptpython REPL with pre-populated mlody namespace."""

from __future__ import annotations

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


def _build_repl_namespace(workspace: Workspace, monorepo_root: Path) -> dict[str, object]:
    """Construct the REPL namespace exposed to the user.

    Exposes `show` as a callable that accepts a raw label string and resolves
    it via show_fn (which handles committoid-qualified labels). `workspace` is
    also exposed directly for advanced inspection of the cwd workspace.
    """
    def _show(*labels: str) -> object | list[object]:
        results = [show_fn(label, monorepo_root=monorepo_root) for label in labels]
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
@click.pass_context
def shell(ctx: click.Context) -> None:
    """Launch an interactive Python REPL with the mlody namespace pre-loaded.

    Available in the REPL:
      show("@root//pkg:target")  — resolve and return a pipeline value
      workspace                  — the loaded Workspace instance
    """
    # Support legacy test injection of a pre-built workspace via ctx.obj
    if "workspace" in ctx.obj:
        workspace: Workspace = ctx.obj["workspace"]
        monorepo_root: Path = ctx.obj.get("monorepo_root", Path.cwd())
        history_file = _get_history_path()
        namespace = _build_repl_namespace(workspace, monorepo_root)
        _launch_repl(namespace, history_file)
        return

    monorepo_root = ctx.obj["monorepo_root"]
    roots: Path | None = ctx.obj.get("roots")

    from mlody.resolver import resolve_workspace

    # Load the cwd workspace to expose as the `workspace` REPL variable.
    # An @-prefixed dummy label with just the root marker is used to trigger
    # the cwd path through resolve_workspace.
    workspace_obj, _sha = resolve_workspace(
        "@//:_shell_init",
        monorepo_root=monorepo_root,
        roots_file=roots,
    )
    history_file = _get_history_path()
    namespace = _build_repl_namespace(workspace_obj, monorepo_root)
    _launch_repl(namespace, history_file)
