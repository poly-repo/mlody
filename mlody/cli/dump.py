"""dump subcommand — emit raw registered entities as JSON."""

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

_RUNTIME_ONLY_ENTITY_FIELDS = frozenset(
    {
        "_context_attr_policies",
        "_entity_type",
        "_producer_task",
        "_source_range",
        "_source_value",
        "lineage",
        "methods",
        "raw",
    }
)


def _iter_workspace_registry_rows(
    workspace: object,
    *,
    kind_filter: str | None = None,
    name_filter: str | None = None,
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
        if kind_filter is not None and raw_kind != kind_filter:
            continue
        if name_filter is not None and raw_name != name_filter:
            continue
        rows.append((raw_kind, raw_stem, raw_name, value))
    rows.sort(key=lambda row: row[:3])
    return tuple(rows)


def _trim_runtime_only_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _trim_runtime_only_fields(child)
            for key, child in value.items()
            if key not in _RUNTIME_ONLY_ENTITY_FIELDS
        }
    if isinstance(value, list):
        return [_trim_runtime_only_fields(child) for child in value]
    return value


def _registered_entity_payload(raw_kind: str, value: object) -> object:
    trimmed_value = _trim_runtime_only_fields(_runtime_json_data(value))
    if isinstance(trimmed_value, dict):
        if "kind" not in trimmed_value:
            return {"kind": raw_kind, **trimmed_value}
        return trimmed_value
    return {"kind": raw_kind, "value": trimmed_value}


def _raw_registry_payload(
    workspace: object,
    *,
    kind_filter: str | None = None,
    name_filter: str | None = None,
) -> list[object]:
    return [
        _registered_entity_payload(raw_kind, value)
        for raw_kind, _stem, _name, value in _iter_workspace_registry_rows(
            workspace,
            kind_filter=kind_filter,
            name_filter=name_filter,
        )
    ]


@cli.command()
@click.option(
    "--kind",
    "kind_filter",
    help="Only dump registered entities of the given kind.",
)
@click.option(
    "--name",
    "name_filter",
    help="Only dump registered entities with the given name.",
)
@click.argument("target", required=False)
@click.pass_context
def dump(
    ctx: click.Context,
    kind_filter: str | None,
    name_filter: str | None,
    target: str | None,
) -> None:
    """Dump raw registered entities before workspace fixups or value resolution.

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

    click.echo(
        json.dumps(
            _raw_registry_payload(
                workspace,
                kind_filter=kind_filter,
                name_filter=name_filter,
            ),
            indent=2,
            sort_keys=True,
        )
    )
