"""Focused tests for concrete workspace anchor behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mlody.core.anchor import (
    ModuleAggregateAnchor,
    ModuleGlobalAnchor,
    RegistryEntityAnchor,
    RootCollectionAnchor,
    RootObjectAnchor,
    WorkspaceAttributeAnchor,
)


@dataclass
class _FakeRegistryWriter:
    registry_entity_writes: list[tuple[tuple[object, object, object], object]] = field(
        default_factory=list
    )
    root_writes: list[tuple[str, object]] = field(default_factory=list)
    module_global_writes: list[tuple[Path, str, object]] = field(default_factory=list)

    def set_registry_entity(
        self,
        key: tuple[object, object, object],
        value: object,
    ) -> None:
        self.registry_entity_writes.append((key, value))

    def set_root_value(self, root_name: str, value: object) -> None:
        self.root_writes.append((root_name, value))

    def set_module_global(
        self,
        file_path: Path,
        symbol_name: str,
        value: object,
    ) -> None:
        self.module_global_writes.append((file_path, symbol_name, value))


def test_registry_entity_anchor_writes_back_to_registry() -> None:
    registry = _FakeRegistryWriter()
    anchor = RegistryEntityAnchor(
        root_value={"kind": "task"},
        registry_key=("task", "mlody/teams/lexica/pipeline", "trainer"),
        field_parts=("outputs",),
    )

    anchor.write_back(registry, {"kind": "task", "updated": True})

    assert registry.registry_entity_writes == [
        (
            ("task", "mlody/teams/lexica/pipeline", "trainer"),
            {"kind": "task", "updated": True},
        )
    ]


def test_root_object_anchor_writes_back_to_root_namespace() -> None:
    registry = _FakeRegistryWriter()
    anchor = RootObjectAnchor(
        root_value={"kind": "root"},
        root_name="bert",
        field_parts=("lr",),
    )

    anchor.write_back(registry, {"kind": "root", "lr": 0.2})

    assert registry.root_writes == [("bert", {"kind": "root", "lr": 0.2})]


def test_module_global_anchor_writes_back_to_module_globals() -> None:
    registry = _FakeRegistryWriter()
    module_path = Path("/workspace/mlody/teams/lexica/module_globals.mlody")
    anchor = ModuleGlobalAnchor(
        root_value={"kind": "value"},
        file_path=module_path,
        symbol_name="global_cfg",
        field_parts=("nested",),
    )

    anchor.write_back(registry, {"kind": "value", "nested": {"answer": 43}})

    assert registry.module_global_writes == [
        (module_path, "global_cfg", {"kind": "value", "nested": {"answer": 43}})
    ]


def test_workspace_attribute_anchor_is_explicitly_read_only() -> None:
    anchor = WorkspaceAttributeAnchor(
        root_value=object(),
        root_attribute="info",
        field_parts=("branch",),
    )

    with pytest.raises(NotImplementedError, match="workspace attribute selectors"):
        anchor.ensure_writable()


def test_module_aggregate_anchor_reports_collection_view_and_rejects_writes() -> None:
    anchor = ModuleAggregateAnchor(
        root_value={"action/trainer": object()},
        root_name="lexica",
        module_stem="mlody/teams/lexica/pipeline",
    )

    assert anchor.exposes_collection_view() is True
    with pytest.raises(NotImplementedError, match="module aggregate assignments"):
        anchor.ensure_writable()


def test_root_collection_anchor_reports_collection_view_and_rejects_writes() -> None:
    anchor = RootCollectionAnchor(root_value={"bert": object()})

    assert anchor.exposes_collection_view() is True
    with pytest.raises(NotImplementedError, match="root collection assignments"):
        anchor.ensure_writable()
