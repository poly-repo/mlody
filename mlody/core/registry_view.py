"""Evaluator-facing registry wrapper used by the mlody Python runtime.

This module is the only production code allowed to touch evaluator internals
such as ``registry.roots.by_name``, ``_module_globals``,
``registry.types.by_name``, and ``registry.all``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from mlody.common.struct import Struct
from mlody.core.anchor import RegistryEntityAnchor
from mlody.core.workspace_models import RootInfo
from common.python.starlarkish.evaluator.evaluator import Evaluator


def _matching_mlody_query_candidates(
    entity_query: str | None,
    candidates: list[tuple[tuple[object, object, object], object]],
) -> list[tuple[tuple[object, object, object], object]]:
    if entity_query is None:
        return []

    from mlody.core.traversal_parser import (  # noqa: PLC0415
        evaluate_mlody_segment,
        parse_mlody_segment,
    )

    segment = parse_mlody_segment(entity_query)
    if segment is None:
        return []

    matches: list[tuple[tuple[object, object, object], object]] = []
    for candidate in candidates:
        try:
            if evaluate_mlody_segment(segment, candidate[1]):
                matches.append(candidate)
        except Exception:
            continue
    return matches


class RegistryView:
    """Small facade over the evaluator's registry state."""

    def __init__(
        self,
        evaluator: Evaluator,
        *,
        workspace_attribute_writer: Callable[[str, object], None] | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._workspace_attribute_writer = workspace_attribute_writer

    def eval_file(self, file_path: Path) -> None:
        self._evaluator.eval_file(file_path)

    def is_loaded(self, file_path: Path) -> bool:
        return file_path in self._evaluator.loaded_files

    def build_root_infos(self) -> dict[str, RootInfo]:
        root_infos: dict[str, RootInfo] = {}
        for _key, root_obj in self._evaluator.registry.roots.by_key.items():
            name = root_obj.name
            root_infos[name] = RootInfo(
                name=name,
                path=getattr(root_obj, "path", ""),
                description=getattr(root_obj, "description", ""),
            )
        return root_infos

    def ensure_root_placeholder(
        self,
        root_name: str,
        root_path: str,
        *,
        description: str = "injected",
    ) -> None:
        placeholder = Struct(
            name=root_name,
            path=root_path,
            description=description,
        )
        self._evaluator.registry.roots.by_name[root_name] = self._evaluator.decorate_registered_value(
            "root",
            placeholder,
        )

    def has_root(self, root_name: str) -> bool:
        return root_name in self._evaluator.registry.roots.by_name

    def available_root_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._evaluator.registry.roots.by_name))

    def propagate_globals_as_persistent_injections(
        self, file_path: Path, names: list[str]
    ) -> None:
        """Copy named symbols from file_path's globals into _persistent_injections.

        _persistent_injections are seeded into every subsequent file's sandbox so
        that the named symbols are visible without an explicit load().  This is how
        mm.mlody makes `mm` and `defmethod` available sandbox-wide after Phase 1.
        """
        file_globals = self._evaluator._module_globals.get(file_path, {})
        for name in names:
            if name in file_globals:
                self._evaluator._persistent_injections[name] = file_globals[name]

    def root_value(self, root_name: str) -> object:
        return self._evaluator.registry.roots.by_name[root_name]

    def root_mapping(self) -> Mapping[str, object]:
        return self._evaluator.registry.roots.by_name

    def root_values_snapshot(self) -> dict[str, object]:
        return dict(self._evaluator.registry.roots.by_name)

    def task_values_snapshot(self) -> dict[str, object]:
        return dict(self._evaluator.registry.tasks.by_name)

    def action_values_snapshot(self) -> dict[str, object]:
        return dict(self._evaluator.registry.actions.by_name)

    def value_values_snapshot(self) -> dict[str, object]:
        return dict(self._evaluator.registry.values.by_name)

    def configs_snapshot(self) -> list[tuple[str, object]]:
        """Return all registered configs sorted by registry key stem (shallower paths first).

        Returns (registry_key, struct) pairs. Sorting by key ensures hierarchical
        application order: configs from shallower package paths apply before
        deeper-path configs, so deeper configs win when both target the same label.
        """
        return sorted(
            self._evaluator.registry.configs.by_key.items(),
            key=lambda item: (item[0].count("/"), item[0]),
        )

    def set_root_value(self, root_name: str, value: object) -> None:
        self._evaluator.registry.roots.by_name[root_name] = self._evaluator.decorate_registered_value(
            "root",
            value,
        )

    def type_by_name(self, type_name: str) -> object | None:
        return self._evaluator.registry.types.by_name.get(type_name)

    def ensure_module_loaded(self, file_path: Path) -> None:
        if not self.is_loaded(file_path):
            self.eval_file(file_path)

    def module_globals(self, file_path: Path) -> dict[str, object]:
        return self._evaluator._module_globals.get(file_path, {})  # type: ignore[return-value]

    def set_module_global(
        self,
        file_path: Path,
        symbol_name: str,
        value: object,
    ) -> None:
        inferred_kind = getattr(value, "kind", None)
        if isinstance(inferred_kind, str):
            value = self._evaluator.decorate_registered_value(inferred_kind, value)
        self._evaluator._module_globals[file_path][symbol_name] = value

    def iter_registry_items(
        self,
    ) -> tuple[tuple[tuple[object, object, object], object], ...]:
        items: list[tuple[tuple[object, object, object], object]] = []
        for key, value in self._evaluator.registry.all.items():
            if isinstance(key, tuple) and len(key) == 3:
                items.append((key, value))
        return tuple(items)

    def set_registry_entity(
        self,
        key: tuple[object, object, object],
        value: object,
    ) -> None:
        inferred_kind = key[0] if isinstance(key[0], str) else None
        if inferred_kind is not None:
            value = self._evaluator.decorate_registered_value(inferred_kind, value)
            bucket = self._evaluator.registry.for_kind(inferred_kind, operation="update")
            stem = key[1] if isinstance(key[1], str) else None
            name = key[2] if isinstance(key[2], str) else None
            by_key_name = f"{stem}:{name}" if stem and name else name
            if by_key_name is not None:
                bucket.by_key[by_key_name] = value
            if name is not None:
                bucket.by_name[name] = value
        self._evaluator.registry.all[key] = value

    def set_workspace_attribute(self, attribute_name: str, value: object) -> None:
        if self._workspace_attribute_writer is None:
            raise NotImplementedError("workspace attribute writes are not configured")
        self._workspace_attribute_writer(attribute_name, value)

    def resolve_all(self) -> None:
        self._evaluator.resolve()

    def debug_dump(self) -> dict[str, object]:
        return {
            str(key): value.to_dict() if hasattr(value, "to_dict") else value
            for key, value in self._evaluator.registry.all.items()
        }

    def match_registry_entity_label(
        self,
        target: str,
        *,
        entity: object,
        entity_query: str | None,
        attribute_path: tuple[str, ...] | None,
        root_infos: Mapping[str, RootInfo],
    ) -> RegistryEntityAnchor | None:
        """Return a registry-backed anchor when the entity is registered."""
        entity_root = getattr(entity, "root", None)
        entity_path = getattr(entity, "path", None)
        entity_name = getattr(entity, "name", None)
        entity_field_path = getattr(entity, "field_path", ()) or ()

        if entity_name is None:
            return None

        name_parts = entity_name.split(".")
        base_name = name_parts[0]
        field_parts = tuple(entity_field_path) if entity_field_path else tuple(name_parts[1:])
        if attribute_path:
            field_parts = field_parts + attribute_path

        stem_parts: list[str] = []
        can_registry_resolve = True
        if entity_root is not None:
            if entity_root in root_infos:
                root_rel = root_infos[entity_root].path.lstrip("/").rstrip("/")
                if root_rel:
                    stem_parts.append(root_rel)
            elif self.has_root(entity_root):
                can_registry_resolve = False
            else:
                available = list(self.available_root_names())
                msg = f"Root {entity_root!r} not found; available roots: {available}"
                raise KeyError(msg)
        if entity_path:
            stem_parts.append(entity_path.lstrip("/").rstrip("/"))
        stem = "/".join([part for part in stem_parts if part])
        path_suffix = entity_path.lstrip("/").rstrip("/") if entity_path else ""
        root_prefix = None
        if entity_root is not None and entity_root in root_infos:
            root_prefix = root_infos[entity_root].path.lstrip("/").rstrip("/")

        matches: list[tuple[tuple[object, object, object], object]] = []
        if can_registry_resolve:
            for key, value in self.iter_registry_items():
                if key[1] == stem and key[2] == base_name:
                    matches.append((key, value))

        if can_registry_resolve and not matches:
            for key, value in self.iter_registry_items():
                key_stem = key[1]
                if key[2] != base_name or not isinstance(key_stem, str):
                    continue
                if root_prefix and not key_stem.startswith(root_prefix):
                    continue
                if path_suffix and not key_stem.endswith(path_suffix):
                    continue
                matches.append((key, value))

        if not matches:
            if can_registry_resolve and entity_root is not None:
                msg = (
                    f"Entity {base_name!r} not found"
                    + (f" in module {stem!r}" if stem else "")
                    + f" (label: {target!r})"
                )
                raise KeyError(msg)
            return None

        mlody_matches = _matching_mlody_query_candidates(entity_query, matches)
        if mlody_matches:
            matches = mlody_matches

        kind_order = {
            "task": 0,
            "action": 1,
            "value": 2,
            "type": 3,
            "location": 4,
            "root": 5,
        }
        matches.sort(key=lambda item: kind_order.get(item[0][0], 99))
        match_key, match_value = matches[0]
        return RegistryEntityAnchor(
            root_value=match_value,
            registry_key=match_key,
            field_parts=field_parts,
            entity_query=entity_query,
        )

    def module_aggregate(self, module_stem: str) -> dict[str, object]:
        return {
            f"{key[0]}/{key[2]}": value
            for key, value in self.iter_registry_items()
            if key[1] == module_stem
        }

    def expand_wildcard_label(
        self,
        inner_label: str,
        *,
        root_infos: Mapping[str, RootInfo],
    ) -> list[str]:
        """Expand a wildcard inner label into concrete labels."""
        from mlody.core.label import parse_label as _core_parse_label
        from mlody.core.label.errors import LabelParseError

        try:
            lbl = _core_parse_label(inner_label)
        except LabelParseError:
            return [inner_label]

        if lbl.entity is None or not lbl.entity.wildcard:
            return [inner_label]

        entity = lbl.entity
        base_name = entity.name.split(".")[0] if entity.name else None

        root_prefix: str | None = None
        if entity.root is not None and entity.root in root_infos:
            root_prefix = root_infos[entity.root].path.lstrip("/").rstrip("/")

        path_suffix = entity.path.lstrip("/").rstrip("/") if entity.path else ""

        candidates: list[tuple[tuple[object, object, object], object]] = []
        for key, value in self.iter_registry_items():
            key_stem, key_name = key[1], key[2]
            if not isinstance(key_stem, str) or not isinstance(key_name, str):
                continue
            if base_name is not None and key_name != base_name:
                continue
            if root_prefix is not None and not key_stem.startswith(root_prefix):
                continue
            if path_suffix and not key_stem.endswith(path_suffix):
                continue
            candidates.append((key, value))

        from mlody.core.traversal_parser import parse_mlody_segment  # noqa: PLC0415

        mlody_segment = parse_mlody_segment(lbl.entity_query)
        mlody_matches = _matching_mlody_query_candidates(lbl.entity_query, candidates)
        if entity.name is None and mlody_segment is not None and not mlody_matches:
            return []
        expand_entities = entity.name is None and mlody_segment is not None

        result: list[str] = []
        seen_labels: set[str] = set()
        concrete_items: list[tuple[str, str]]
        if expand_entities:
            concrete_items = sorted(
                {
                    (key[1], key[2])
                    for key, _value in mlody_matches
                    if isinstance(key[1], str) and isinstance(key[2], str)
                },
            )
        else:
            concrete_items = sorted(
                {
                    (key[1], "")
                    for key, _value in candidates
                    if isinstance(key[1], str)
                },
            )

        for stem, concrete_name in concrete_items:
            if root_prefix and stem.startswith(root_prefix):
                rel_path = stem[len(root_prefix) :].lstrip("/")
            else:
                rel_path = stem

            parts: list[str] = []
            if entity.root:
                parts.append(f"@{entity.root}//{rel_path}")
            else:
                parts.append(f"//{rel_path}")
            if entity.name:
                field_suffix = (
                    "." + ".".join(entity.field_path) if entity.field_path else ""
                )
                query_suffix = f"[{lbl.entity_query}]" if lbl.entity_query else ""
                parts.append(f":{entity.name}{field_suffix}{query_suffix}")
            elif expand_entities:
                field_suffix = (
                    "." + ".".join(entity.field_path) if entity.field_path else ""
                )
                query_suffix = f"[{lbl.entity_query}]" if lbl.entity_query else ""
                parts.append(f":{concrete_name}{field_suffix}{query_suffix}")
            elif lbl.entity_query:
                parts.append(f":[{lbl.entity_query}]")
            if lbl.attribute_path:
                parts.append(f"'{'.'.join(lbl.attribute_path)}")
            rendered = "".join(parts)
            if rendered in seen_labels:
                continue
            seen_labels.add(rendered)
            result.append(rendered)

        return result
