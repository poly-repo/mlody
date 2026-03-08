"""CLI entry point for mlody — click group with global options and monorepo root verification."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click
from rich.logging import RichHandler


def _configure_logging(verbose: bool) -> None:
    """Configure console logging for the CLI.

    Sets the root logger level on every invocation so that --verbose takes
    effect even when the process already has handlers (e.g. in tests).
    Attaches a RichHandler only when no handlers are present yet — in
    production the root logger starts empty; in tests pytest has already
    installed its own capture handler which we leave in place.

    Rich is used unconditionally for now; conditionality (TTY detection,
    availability check) will be added in a follow-up.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(RichHandler(rich_tracebacks=True, show_path=False))


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


@click.group()
@click.option(
    "--roots",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to roots.mlody (default: mlody/roots.mlody)",
)
@click.option("--verbose", is_flag=True, default=False, help="Increase output verbosity")
@click.pass_context
def cli(ctx: click.Context, roots: Path | None, verbose: bool) -> None:
    """mlody — ML pipeline framework CLI."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _configure_logging(verbose)

    # Allow tests to inject pre-built context objects without triggering
    # filesystem verification. Tests may inject either workspace (legacy) or
    # monorepo_root (new-style) to bypass the verify step.
    if "monorepo_root" in ctx.obj or "workspace" in ctx.obj:
        return

    monorepo_root = verify_monorepo_root()
    ctx.obj["monorepo_root"] = monorepo_root
    ctx.obj["roots"] = roots


def main() -> None:
    """Entry point. Import subcommands and invoke the CLI group."""
    import mlody.cli.shell  # noqa: F401
    import mlody.cli.show  # noqa: F401

    cli()
