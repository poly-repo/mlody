"""Evaluator-facing registry wrapper used by the mlody Python runtime.

This module is the only production code allowed to touch evaluator internals
such as ``_roots_by_name``, ``_module_globals``, ``_types_by_name``, and
``all``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from mlody.common.struct import Struct
from mlody.core.anchor import RegistryEntityAnchor
from mlody.core.workspace_models import RootInfo
from common.python.starlarkish.evaluator.evaluator import Evaluator


class RegistryView:
    """Small facade over the evaluator's registry state."""

    def __init__(self, evaluator: Evaluator) -> None:
        self._evaluator = evaluator

    def eval_file(self, file_path: Path) -> None:
        self._evaluator.eval_file(file_path)

    def is_loaded(self, file_path: Path) -> bool:
        return file_path in self._evaluator.loaded_files

    def build_root_infos(self) -> dict[str, RootInfo]:
        root_infos: dict[str, RootInfo] = {}
        for _key, root_obj in self._evaluator.roots.items():
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
        self._evaluator._roots_by_name[root_name] = Struct(
            name=root_name,
            path=root_path,
            description=description,
        )

    def has_root(self, root_name: str) -> bool:
        return root_name in self._evaluator._roots_by_name

    def available_root_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._evaluator._roots_by_name))

    def root_value(self, root_name: str) -> object:
        return self._evaluator._roots_by_name[root_name]

    def root_mapping(self) -> Mapping[str, object]:
        return self._evaluator._roots_by_name

    def root_values_snapshot(self) -> dict[str, object]:
        return dict(self._evaluator._roots_by_name)

    def set_root_value(self, root_name: str, value: object) -> None:
        self._evaluator._roots_by_name[root_name] = value

    def type_by_name(self, type_name: str) -> object | None:
        return self._evaluator._types_by_name.get(type_name)

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
        self._evaluator._module_globals[file_path][symbol_name] = value

    def iter_registry_items(
        self,
    ) -> tuple[tuple[tuple[object, object, object], object], ...]:
        items: list[tuple[tuple[object, object, object], object]] = []
        for key, value in self._evaluator.all.items():
            if isinstance(key, tuple) and len(key) == 3:
                items.append((key, value))
        return tuple(items)

    def set_registry_entity(
        self,
        key: tuple[object, object, object],
        value: object,
    ) -> None:
        self._evaluator.all[key] = value

    def resolve_all(self) -> None:
        self._evaluator.resolve()

    def debug_dump(self) -> dict[str, object]:
        return {
            str(key): value.to_dict() if hasattr(value, "to_dict") else value
            for key, value in self._evaluator.all.items()
        }

    def match_registry_entity_label(
        self,
        target: str,
        *,
        entity: object,
        attribute_path: tuple[str, ...] | None,
        root_infos: Mapping[str, RootInfo],
    ) -> RegistryEntityAnchor | None:
        """Return a registry-backed anchor when the entity is registered."""
        entity_root = getattr(entity, "root", None)
        entity_path = getattr(entity, "path", None)
        entity_name = getattr(entity, "name", None)
        entity_field_path = getattr(entity, "field_path", ()) or ()
        entity_query = getattr(entity, "entity_query", None)

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

        stems: set[str] = set()
        for key, _value in self.iter_registry_items():
            key_stem, key_name = key[1], key[2]
            if not isinstance(key_stem, str):
                continue
            if base_name is not None and key_name != base_name:
                continue
            if root_prefix is not None and not key_stem.startswith(root_prefix):
                continue
            if path_suffix and not key_stem.endswith(path_suffix):
                continue
            stems.add(key_stem)

        result: list[str] = []
        for stem in sorted(stems):
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
            if lbl.attribute_path:
                parts.append(f"'{'.'.join(lbl.attribute_path)}")
            result.append("".join(parts))

        return result
