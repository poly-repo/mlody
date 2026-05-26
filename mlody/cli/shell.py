"""shell subcommand — restricted ptpython REPL with mlody session globals."""

from __future__ import annotations

from io import StringIO
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console
from rich.pretty import Pretty
from rich.pretty import pretty_repr

from mlody.cli.main import cli
from mlody.cli.show import show_fn
from mlody.common.struct import is_struct_like, struct_like_as_mapping
from mlody.core.workspace import Workspace
from mlody.starlark import make_actions_struct

if TYPE_CHECKING:
    pass


_SHELL_RESULT_MAX_DEPTH = 4


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


def _session_file_path(monorepo_root: Path, workspace_root: Path | None = None) -> Path:
    root = workspace_root if workspace_root is not None else monorepo_root
    return root / "__shell_session__.mlody"


def _build_shell_session_globals(
    workspace: Workspace,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    full_workspace: bool = False,
) -> dict[str, object]:
    shell_helpers = _build_repl_namespace(
        workspace,
        monorepo_root,
        workspace_root,
        full_workspace,
    )
    return workspace.registry_view.host_session_globals(
        _session_file_path(monorepo_root, workspace_root),
        initial_globals=shell_helpers,
    )


class _PrettyPrintedShellResult:
    def __init__(self, rendered: object) -> None:
        self._rendered = rendered

    def __pt_repr__(self) -> object:
        return self._rendered


def _normalize_shell_result_value(value: object) -> object:
    if is_struct_like(value):
        return {
            key: _normalize_shell_result_value(child)
            for key, child in struct_like_as_mapping(value).items()
        }
    if isinstance(value, dict):
        return {key: _normalize_shell_result_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_shell_result_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_normalize_shell_result_value(child) for child in value)
    if callable(value) and not isinstance(value, type):
        return "<function>"
    return value


def _format_shell_result(
    result: object,
    *,
    max_width: int = 88,
    max_depth: int = _SHELL_RESULT_MAX_DEPTH,
) -> str:
    return pretty_repr(
        _normalize_shell_result_value(result),
        max_width=max_width,
        max_depth=max_depth,
    )


def _format_shell_result_ansi(
    result: object,
    *,
    max_width: int = 88,
    max_depth: int = _SHELL_RESULT_MAX_DEPTH,
) -> object:
    from prompt_toolkit.formatted_text import ANSI

    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        width=max_width,
        highlight=True,
        soft_wrap=False,
    )
    console.print(
        Pretty(
            _normalize_shell_result_value(result),
            max_depth=max_depth,
        )
    )
    return ANSI(buffer.getvalue().rstrip("\n"))


def _configure_result_pretty_printer(repl: Any) -> None:  # pyright: ignore[reportExplicitAny]
    from prompt_toolkit.formatted_text import fragment_list_width, to_formatted_text

    default_show_result = repl._show_result

    def _show_result(result: object) -> None:
        try:
            printer = repl._get_output_printer()
            out_prompt = to_formatted_text(repl.get_output_prompt())
            width = max(
                20,
                printer.output.get_size().columns - fragment_list_width(out_prompt),
            )
            rendered = _format_shell_result_ansi(result, max_width=width)
            printer.display_result(
                result=_PrettyPrintedShellResult(rendered),
                out_prompt=out_prompt,
                reformat=False,
                highlight=False,
                paginate=repl.enable_pager,
            )
        except (GeneratorExit, KeyboardInterrupt):
            raise
        except BaseException:
            default_show_result(result)

    repl._show_result = _show_result


def _launch_repl(session_globals: dict[str, object], history_file: Path) -> None:
    """Launch the ptpython REPL with the given restricted session globals.

    Isolated as a separate function to act as a test seam — callers mock this
    to verify namespace construction without starting an interactive process.
    """
    from ptpython.repl import embed  # deferred import — ptpython is heavyweight

    embed(
        globals=session_globals,  # type: ignore[arg-type]  # dict[str, object] ≈ dict[str, Any]
        locals=session_globals,  # type: ignore[arg-type]  # dict[str, object] ≈ dict[str, Any]
        configure=_configure_result_pretty_printer,
        history_filename=str(history_file),
        title="mlody shell",
    )


@cli.command()
@click.option(
    "--load",
    "eval_files",
    type=click.Path(path_type=Path, dir_okay=False),
    multiple=True,
    help=(
        "Inject a .mlody file as a monorepo-root module before starting the shell. "
        "Repeatable: --load a.mlody --load b.mlody"
    ),
)
@click.option(
    "--as",
    "run_as",
    default=None,
    help="Registered user name to set as the active workspace user.",
)
@click.pass_context
def shell(ctx: click.Context, eval_files: tuple[Path, ...], run_as: str | None) -> None:
    """Launch a restricted Starlarkish-safe Python REPL with mlody session globals.

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
        session_globals = _build_shell_session_globals(
            workspace,
            monorepo_root,
            workspace_root,
            full_workspace,
        )
        _launch_repl(session_globals, history_file)
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
            click.echo(f"Error: --load file not found: {p}", err=True)
            sys.exit(1)
        resolved_eval_files.append(p)

    from mlody.resolver import resolve_workspace

    workspace_obj, _sha = resolve_workspace(
        "//mlody:_shell_init",
        monorepo_root=monorepo_root,
        workspace_root=workspace_root,
        roots_file=roots,
        full_workspace=full_workspace,
        user=run_as,
    )
    workspace_obj.registry_view.inject_persistent("actions", make_actions_struct())
    history_file = _get_history_path()
    session_globals = _build_shell_session_globals(
        workspace_obj,
        monorepo_root,
        workspace_root,
        full_workspace,
    )
    prelude_path = monorepo_root / "mlody" / "shell" / "prelude.mlody"
    if prelude_path.exists():
        workspace_obj.registry_view.eval_file(prelude_path)
        prelude_globals = workspace_obj.registry_view.host_module_globals(prelude_path)
        # Inject all prelude symbols as persistent injections so that any
        # subsequently evaluated file (e.g. --eval-file scripts) can use them
        # without an explicit load().
        workspace_obj.registry_view.propagate_globals_as_persistent_injections(
            prelude_path, [k for k in prelude_globals if not k.startswith("_")]
        )
        session_globals.update(prelude_globals)
    for ef in resolved_eval_files:
        virtual_path = monorepo_root / ef.name
        workspace_obj.registry_view.register_path_redirect(virtual_path, ef)
        workspace_obj.registry_view.eval_file(virtual_path)
        session_globals.update(workspace_obj.registry_view.host_module_globals(virtual_path))
    _launch_repl(session_globals, history_file)
