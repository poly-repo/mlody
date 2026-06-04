"""dump subcommand — emit raw registered entities as JSON."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

import click

from mlody.cli.main import cli
from mlody.common._registered_struct import RegisteredStructBase
from mlody.common.struct import Struct
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


def _is_kind(value: object, expected_kind: str) -> bool:
    if isinstance(value, RegisteredStructBase):
        return value.kind == expected_kind
    if isinstance(value, Struct):
        return value.as_mapping().get("kind") == expected_kind
    if isinstance(value, dict):
        return value.get("kind") == expected_kind
    return False


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


def _struct_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, RegisteredStructBase):
        return value.as_mapping()
    if isinstance(value, Struct):
        return value.as_mapping()
    if isinstance(value, dict):
        return value
    return None


def _compact_singleton_list(value: object) -> object:
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def _source_like_json_data(
    value: object,
    *,
    field_name: str | None = None,
    _seen: set[int] | None = None,
) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if callable(value) and not isinstance(value, type):
        return "<callable>"

    if _seen is None:
        _seen = set()

    is_container_like = isinstance(
        value,
        (RegisteredStructBase, Struct, dict, list, tuple, set),
    )
    value_id = id(value)
    if is_container_like:
        if value_id in _seen:
            return "<cycle>"
        _seen.add(value_id)

    try:
        mapping = _struct_mapping(value)
        if mapping is not None:
            kind = mapping.get("kind")
            if kind == "task":
                return _compact_task_mapping(mapping, _seen=_seen)
            if kind == "action":
                return _compact_action_mapping(mapping, _seen=_seen)
            if kind == "value":
                return _compact_value_mapping(mapping, _seen=_seen)
            if kind == "type":
                return _compact_type_mapping(mapping, _seen=_seen)
            if kind == "location":
                return _compact_location_mapping(mapping, _seen=_seen)
            if kind == "freshness":
                return _compact_freshness_mapping(mapping, _seen=_seen)
            if kind == "representation":
                return _compact_representation_mapping(mapping, _seen=_seen)
            if kind == "build_ref":
                return _compact_build_ref_mapping(mapping, _seen=_seen)
            if kind == "requirement":
                return _compact_requirement_mapping(mapping, _seen=_seen)
            return {
                str(key): _source_like_json_data(child, field_name=str(key), _seen=_seen)
                for key, child in mapping.items()
                if key not in _RUNTIME_ONLY_ENTITY_FIELDS
            }
        if isinstance(value, dict):
            return {
                str(key): _source_like_json_data(child, field_name=str(key), _seen=_seen)
                for key, child in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [
                _source_like_json_data(child, field_name=field_name, _seen=_seen)
                for child in value
            ]
        if hasattr(value, "__dict__"):
            return {
                str(key): _source_like_json_data(child, field_name=str(key), _seen=_seen)
                for key, child in vars(value).items()
                if key not in {"raw", "_entity_type", "_evaluator", "evaluator"}
            }
        return repr(value)
    finally:
        if is_container_like:
            _seen.remove(value_id)


def _compact_named_value_collection(value: object, *, _seen: set[int]) -> list[object]:
    if value is None:
        return []
    if isinstance(value, dict):
        items = value.values()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return [_source_like_json_data(value, _seen=_seen)]
    return [_source_like_json_data(item, _seen=_seen) for item in items]


def _compact_type_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> object:
    type_name = mapping.get("type") or mapping.get("name")
    attributes = mapping.get("attributes")
    result: dict[str, object] = {}
    if isinstance(type_name, str) and type_name:
        result["type"] = type_name
    if isinstance(attributes, dict):
        for key, child in attributes.items():
            result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    if not result:
        return "type"
    if len(result) == 1 and "type" in result:
        return result["type"]
    return result


def _compact_location_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> object:
    location_type = mapping.get("type") or mapping.get("name")
    result: dict[str, object] = {}
    if isinstance(location_type, str) and location_type:
        result["type"] = location_type
    attributes = mapping.get("attributes")
    if isinstance(attributes, dict):
        for key, child in attributes.items():
            serialized = _source_like_json_data(child, field_name=str(key), _seen=_seen)
            if key == "path":
                serialized = _compact_singleton_list(serialized)
            result[str(key)] = serialized
    data_value = mapping.get("data")
    if data_value is not None:
        result["data"] = _source_like_json_data(data_value, field_name="data", _seen=_seen)
    if len(result) == 1 and "type" in result:
        return result["type"]
    return result


def _compact_freshness_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> object:
    freshness_type = mapping.get("type") or mapping.get("name")
    attributes = mapping.get("attributes")
    if not isinstance(attributes, dict) or len(attributes) == 0:
        return freshness_type if isinstance(freshness_type, str) else "freshness"
    result: dict[str, object] = {"type": freshness_type}
    for key, child in attributes.items():
        result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    return result


def _compact_representation_mapping(
    mapping: Mapping[str, object],
    *,
    _seen: set[int],
) -> object:
    representation_name = mapping.get("name")
    result: dict[str, object] = {}
    if isinstance(representation_name, str) and representation_name:
        result["name"] = representation_name
    attributes = mapping.get("attributes")
    if isinstance(attributes, dict):
        for key, child in attributes.items():
            result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    for key in (
        "schema",
        "multifile",
        "min_length",
        "max_length",
        "total_min_length",
        "total_max_length",
        "separator",
        "header_required",
        "markup",
    ):
        child = mapping.get(key)
        if child is not None:
            result[key] = _source_like_json_data(child, field_name=key, _seen=_seen)
    if len(result) == 1 and "name" in result:
        return result["name"]
    return result


def _compact_build_ref_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> object:
    build_type = mapping.get("type") or mapping.get("name")
    result: dict[str, object] = {}
    if isinstance(build_type, str) and build_type:
        result["type"] = build_type
    target = mapping.get("target")
    if target is not None:
        result["target"] = _source_like_json_data(target, field_name="target", _seen=_seen)
    return result


def _compact_requirement_mapping(
    mapping: Mapping[str, object],
    *,
    _seen: set[int],
) -> dict[str, object]:
    result = {
        str(key): _source_like_json_data(child, field_name=str(key), _seen=_seen)
        for key, child in mapping.items()
        if key not in {"kind"} and key not in _RUNTIME_ONLY_ENTITY_FIELDS
    }
    if result.get("type") == "*":
        result.pop("type")
    return result


def _compact_value_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "value",
        "name": str(mapping.get("name", "")),
    }
    description = mapping.get("description")
    if isinstance(description, str) and description:
        result["description"] = description

    type_value = mapping.get("type")
    if type_value is not None:
        compact_type = _source_like_json_data(type_value, field_name="type", _seen=_seen)
        if compact_type != "nothing":
            result["type"] = compact_type

    location_value = mapping.get("location")
    if location_value is not None:
        compact_location = _source_like_json_data(
            location_value,
            field_name="location",
            _seen=_seen,
        )
        if compact_location != "inline":
            result["location"] = compact_location

    freshness_value = mapping.get("freshness")
    if freshness_value is not None:
        compact_freshness = _source_like_json_data(
            freshness_value,
            field_name="freshness",
            _seen=_seen,
        )
        if compact_freshness != "always":
            result["freshness"] = compact_freshness

    for key in ("unit", "default", "source", "representation", "group", "constraint"):
        child = mapping.get(key)
        if child is not None:
            result[key] = _source_like_json_data(child, field_name=key, _seen=_seen)
    for key, child in mapping.items():
        if key in result or key in {
            "kind",
            "name",
            "description",
            "type",
            "location",
            "freshness",
            "unit",
            "default",
            "source",
            "representation",
            "group",
            "constraint",
        }:
            continue
        if key in _RUNTIME_ONLY_ENTITY_FIELDS:
            continue
        result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    return result


def _compact_task_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "task",
        "name": str(mapping.get("name", "")),
    }
    description = mapping.get("description")
    if isinstance(description, str) and description:
        result["description"] = description
    for field_name in ("inputs", "outputs", "config"):
        items = _compact_named_value_collection(mapping.get(field_name), _seen=_seen)
        if items:
            result[field_name] = items
    action_value = mapping.get("action")
    if action_value is not None:
        result["action"] = _source_like_json_data(action_value, field_name="action", _seen=_seen)
    execution_value = mapping.get("execution")
    if execution_value is not None:
        result["execution"] = _source_like_json_data(
            execution_value,
            field_name="execution",
            _seen=_seen,
        )
    for key, child in mapping.items():
        if key in result or key in {
            "kind",
            "name",
            "description",
            "inputs",
            "outputs",
            "action",
            "config",
            "execution",
        }:
            continue
        if key in _RUNTIME_ONLY_ENTITY_FIELDS:
            continue
        result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    return result


def _compact_action_mapping(mapping: Mapping[str, object], *, _seen: set[int]) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "action",
        "name": str(mapping.get("name", "")),
    }
    description = mapping.get("description")
    if isinstance(description, str) and description:
        result["description"] = description
    for field_name in ("inputs", "outputs", "config"):
        items = _compact_named_value_collection(mapping.get(field_name), _seen=_seen)
        if items:
            result[field_name] = items
    requirements_value = mapping.get("requirements")
    if isinstance(requirements_value, (list, tuple)) and len(requirements_value) > 0:
        result["requirements"] = [
            _source_like_json_data(item, field_name="requirements", _seen=_seen)
            for item in requirements_value
        ]
    for field_name in ("implementation", "build"):
        child = mapping.get(field_name)
        if child is not None:
            result[field_name] = _source_like_json_data(child, field_name=field_name, _seen=_seen)
    for key, child in mapping.items():
        if key in result or key in {
            "kind",
            "name",
            "description",
            "inputs",
            "outputs",
            "config",
            "requirements",
            "implementation",
            "build",
        }:
            continue
        if key in _RUNTIME_ONLY_ENTITY_FIELDS:
            continue
        result[str(key)] = _source_like_json_data(child, field_name=str(key), _seen=_seen)
    return result


def _registered_entity_payload(raw_kind: str, value: object) -> object:
    compact_value = _source_like_json_data(value)
    if isinstance(compact_value, dict):
        if "kind" not in compact_value:
            return {"kind": raw_kind, **compact_value}
        return compact_value
    return {"kind": raw_kind, "value": compact_value}


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
