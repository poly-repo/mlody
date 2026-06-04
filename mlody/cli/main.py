"""CLI entry point for mlody — click group with global options and monorepo root verification."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click
from rich.logging import RichHandler


def _configure_logging(verbose: bool, *, server_mode: bool = False) -> None:
    """Configure console logging for the CLI.

    Sets the root logger level on every invocation so that --verbose takes
    effect even when the process already has handlers (e.g. in tests).
    Attaches a RichHandler only when no handlers are present yet — in
    production the root logger starts empty; in tests pytest has already
    installed its own capture handler which we leave in place.

    Server mode lowers the root logger threshold to DEBUG so the per-request
    stage log collector can retain debug/info records for the web UI. The
    console handler remains at WARNING unless --verbose was requested.

    Rich is used unconditionally for now; conditionality (TTY detection,
    availability check) will be added in a follow-up.
    """
    level = logging.DEBUG if (verbose or server_mode) else logging.WARNING
    console_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = RichHandler(rich_tracebacks=True, show_path=False)
        handler.setLevel(console_level)
        root.addHandler(handler)


def verify_monorepo_root() -> Path:
    """Verify CWD contains MODULE.bazel and return the monorepo root path.

    Exits with code 1 if not at the monorepo root.
    """
    workspace_root = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace_root is None:
        cwd = Path.cwd()
    else:
        cwd = Path(workspace_root)

    if not (cwd / "MODULE.bazel").exists():
        click.echo(
            "Error: mlody must be run from the monorepo root "
            "(expected MODULE.bazel in current directory). "
            "Please cd to the repo root and try again.",
            err=True,
        )
        sys.exit(1)
    return cwd


@click.group(invoke_without_command=True)
@click.option(
    "--roots",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to roots.mlody (default: mlody/roots.mlody)",
)
@click.option("--verbose", is_flag=True, default=False, help="Increase output verbosity")
@click.option(
    "--full-workspace",
    is_flag=True,
    default=False,
    help="Load all .mlody files, including files normally skipped by default.",
)
@click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Workspace subdirectory relative to the monorepo root "
        "(e.g. mlody/sandboxes/exp1). Sets // for CWD-relative labels."
    ),
)
@click.option(
    "--server",
    "server_mode",
    is_flag=True,
    default=False,
    help="Run persistent server mode with HTTP JSON and TCP LSP endpoints.",
)
@click.option(
    "--server-host",
    default="127.0.0.1",
    show_default=True,
    help="Bind host for both the HTTP API and TCP LSP server.",
)
@click.option(
    "--server-port",
    type=click.IntRange(1, 65535),
    default=8765,
    show_default=True,
    help="Bind port for the HTTP JSON API.",
)
@click.option(
    "--lsp-port",
    type=click.IntRange(1, 65535),
    default=8766,
    show_default=True,
    help="Bind port for the TCP LSP server.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    roots: Path | None,
    verbose: bool,
    full_workspace: bool,
    workspace_dir: Path | None,
    server_mode: bool,
    server_host: str,
    server_port: int,
    lsp_port: int,
) -> None:
    """mlody — ML pipeline framework CLI."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["full_workspace"] = full_workspace
    _configure_logging(verbose, server_mode=server_mode)

    # Allow tests to inject pre-built context objects without triggering
    # filesystem verification. Tests may inject either workspace (legacy) or
    # monorepo_root (new-style) to bypass the verify step.
    if "monorepo_root" in ctx.obj or "workspace" in ctx.obj:
        return

    monorepo_root = verify_monorepo_root()
    ctx.obj["monorepo_root"] = monorepo_root
    ctx.obj["roots"] = roots
    if workspace_dir is not None:
        workspace_root = monorepo_root / workspace_dir
        if not workspace_root.is_dir():
            click.echo(
                f"Error: workspace directory not found: {workspace_root}",
                err=True,
            )
            sys.exit(1)
        ctx.obj["workspace_root"] = workspace_root
    else:
        ctx.obj["workspace_root"] = monorepo_root

    if server_mode and ctx.invoked_subcommand is not None:
        raise click.UsageError("--server cannot be combined with a subcommand.")

    if server_mode:
        from mlody.cli.server import ServerConfig, run_server

        run_server(
            ServerConfig(
                monorepo_root=monorepo_root,
                workspace_root=ctx.obj["workspace_root"],
                roots=roots,
                verbose=verbose,
                full_workspace=full_workspace,
                http_host=server_host,
                http_port=server_port,
                lsp_host=server_host,
                lsp_port=lsp_port,
            )
        )
        ctx.exit(0)

    if ctx.invoked_subcommand is None and not ctx.resilient_parsing:
        click.echo(ctx.get_help())
        ctx.exit(0)


def main() -> None:
    """Entry point. Import subcommands and invoke the CLI group."""
    import mlody.cli.db  # noqa: F401
    import mlody.cli.dump  # noqa: F401
    import mlody.cli.shell  # noqa: F401
    import mlody.cli.show  # noqa: F401

    cli()
