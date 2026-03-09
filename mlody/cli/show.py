"""show subcommand — resolve and display pipeline values."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

import click
from rich.pretty import pretty_repr

from mlody.cli.main import cli
from mlody.core.workspace import Workspace, WorkspaceLoadError
from mlody.resolver import resolve_workspace
from mlody.resolver.errors import WorkspaceResolutionError

_logger = logging.getLogger(__name__)



def show_fn(
    label: str,
    monorepo_root: Path,
    roots_file: Path | None = None,
    print_fn: Callable[..., None] = print,
) -> object:
    """Resolve a label to a value via a fresh workspace.

    Used by the shell REPL. Accepts a raw label (with optional committoid prefix)
    and constructs a workspace independently for each call.
    """
    workspace, _sha = resolve_workspace(
        label,
        monorepo_root=monorepo_root,
        roots_file=roots_file,
        print_fn=print_fn,
    )
    _committoid, inner_label = _parse_inner(label)
    return workspace.resolve(inner_label)


def _parse_inner(label: str) -> tuple[str | None, str]:
    """Extract committoid and inner label without raising — delegates to parse_label."""
    from mlody.resolver.resolver import parse_label

    return parse_label(label)


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

    TARGETS: One or more Bazel-style target references. A target may be
    prefixed with a committoid and '|' separator (e.g. main|@root//pkg:tgt)
    to resolve against a specific commit rather than the current workspace.
    """
    # Support legacy test injection of a pre-built workspace via ctx.obj
    if "workspace" in ctx.obj:
        _show_with_legacy_workspace(ctx, targets)
        return

    monorepo_root: Path = ctx.obj["monorepo_root"]
    roots: Path | None = ctx.obj.get("roots")
    has_error = False

    for target in targets:
        try:
            workspace, resolved_sha = resolve_workspace(
                target,
                monorepo_root=monorepo_root,
                roots_file=roots,
            )
            if resolved_sha is not None:
                _logger.debug("Resolved %s to %s", target.split("|")[0], resolved_sha)

            _committoid, inner_label = _parse_inner(target)
            value = workspace.resolve(inner_label)
        except WorkspaceLoadError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue
        except WorkspaceResolutionError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue
        except KeyError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            if hasattr(workspace, "root_infos"):
                available = list(workspace.root_infos.keys())
                if available:
                    click.echo(
                        click.style(f"Available roots: {', '.join(available)}", fg="red"),
                        err=True,
                    )
            continue
        except AttributeError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue

        click.echo(_format_value(value))

    if has_error:
        sys.exit(1)


def _show_with_legacy_workspace(
    ctx: click.Context, targets: tuple[str, ...]
) -> None:
    """Handle the legacy test injection path where ctx.obj['workspace'] is set.

    This path is used by existing tests that inject a pre-built workspace mock.
    It preserves backward compatibility for those tests.
    """
    workspace: Workspace = ctx.obj["workspace"]
    has_error = False

    for target in targets:
        try:
            value = workspace.resolve(target)
        except KeyError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            available = list(workspace.root_infos.keys())
            if available:
                click.echo(
                    click.style(f"Available roots: {', '.join(available)}", fg="red"),
                    err=True,
                )
            continue
        except AttributeError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue

        click.echo(_format_value(value))

    if has_error:
        sys.exit(1)
