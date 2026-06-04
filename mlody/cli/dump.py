"""dump subcommand — emit the raw ``registry.all`` contents as JSON."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

import click

from common.python.starlarkish.evaluator.evaluator import _runtime_json_data
from mlody.cli.main import cli
from mlody.core.workspace import WorkspaceLoadError
from mlody.resolver import resolve_workspace_raw
from mlody.resolver.errors import WorkspaceResolutionError


def _iter_workspace_registry_rows(
    workspace: object,
) -> tuple[tuple[str, str, str, object], ...]:
    registry_view = getattr(workspace, "registry_view", None)
    iter_registry_items = getattr(registry_view, "iter_registry_items", None)
    if callable(iter_registry_items):
        raw_items = iter_registry_items()
    else:
        registry = getattr(getattr(workspace, "evaluator", None), "registry", None)
        all_items = getattr(registry, "all", None)
        raw_items = all_items.items() if isinstance(all_items, Mapping) else ()

    rows: list[tuple[str, str, str, object]] = []
    for raw_key, value in raw_items:
        if not isinstance(raw_key, tuple) or len(raw_key) != 3:
            continue
        raw_kind, raw_stem, raw_name = raw_key
        if (
            not isinstance(raw_kind, str)
            or not isinstance(raw_stem, str)
            or not isinstance(raw_name, str)
        ):
            continue
        rows.append((raw_kind, raw_stem, raw_name, value))
    rows.sort(key=lambda row: row[:3])
    return tuple(rows)


def _raw_registry_payload(workspace: object) -> Mapping[str, object]:
    return {
        "all": [
            {
                "kind": raw_kind,
                "stem": stem,
                "name": name,
                "value": _runtime_json_data(value),
            }
            for raw_kind, stem, name, value in _iter_workspace_registry_rows(workspace)
        ]
    }


@cli.command()
@click.argument("target", required=False)
@click.pass_context
def dump(ctx: click.Context, target: str | None) -> None:
    """Dump raw registry entries before workspace fixups or value resolution.

    TARGET may be omitted to dump the current workspace, or provided using the
    same ref syntax accepted by ``show`` to select a specific cached commit.
    """
    monorepo_root: Path = ctx.obj["monorepo_root"]
    workspace_root: Path = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    verbose: bool = ctx.obj.get("verbose", False)
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    try:
        workspace, _resolved_sha = resolve_workspace_raw(
            target,
            monorepo_root=monorepo_root,
            workspace_root=workspace_root,
            roots_file=roots,
            full_workspace=full_workspace,
            print_fn=click.echo,
            verbose=verbose,
        )
    except WorkspaceLoadError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        ctx.exit(1)
    except WorkspaceResolutionError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        ctx.exit(1)

    click.echo(json.dumps(_raw_registry_payload(workspace), indent=2, sort_keys=True))
