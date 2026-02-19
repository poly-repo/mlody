"""show subcommand — resolve and display pipeline values."""

from __future__ import annotations

import sys

import click
from rich.pretty import pretty_repr

from mlody.cli.main import cli
from mlody.core.workspace import Workspace


def show_fn(workspace: Workspace, *targets: str) -> object | list[object]:
    """Resolve targets and return values. Single target returns one value, multiple returns a list."""
    results = [workspace.resolve(t) for t in targets]
    if len(results) == 1:
        return results[0]
    return results


def _is_primitive(value: object) -> bool:
    return isinstance(value, str | int | float | bool)


def _format_value(value: object) -> str:
    if _is_primitive(value):
        return str(value)
    return pretty_repr(value)


@cli.command()
@click.argument("targets", nargs=-1, required=True)
@click.pass_context
def show(ctx: click.Context, targets: tuple[str, ...]) -> None:
    """Resolve and display pipeline values.

    TARGETS: One or more Bazel-style target references.
    """
    workspace: Workspace = ctx.obj["workspace"]
    has_error = False

    for target in targets:
        try:
            value = show_fn(workspace, target)
        except KeyError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            available = list(workspace.root_infos.keys())
            if available:
                click.echo(click.style(f"Available roots: {', '.join(available)}", fg="red"), err=True)
            continue
        except AttributeError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue

        click.echo(_format_value(value))

    if has_error:
        sys.exit(1)
