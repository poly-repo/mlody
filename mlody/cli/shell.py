"""shell subcommand — interactive ptpython REPL with pre-populated mlody namespace."""

from __future__ import annotations

import functools
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


def _build_repl_namespace(workspace: Workspace) -> dict[str, object]:
    """Construct the REPL namespace exposed to the user.

    Exposes `show` as a partial over show_fn with workspace already closed over,
    so shell users call `show("@root//pkg:target")` rather than passing workspace
    explicitly. `workspace` is also exposed directly for advanced inspection.
    """
    return {
        "show": functools.partial(show_fn, workspace),
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
    workspace: Workspace = ctx.obj["workspace"]
    history_file = _get_history_path()
    namespace = _build_repl_namespace(workspace)
    _launch_repl(namespace, history_file)
