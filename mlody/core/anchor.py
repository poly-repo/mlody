"""Concrete writable anchors for label-resolved workspace targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RegistryWriter(Protocol):
    """Small protocol for the registry mutations needed by writable anchors."""

    def set_registry_entity(
        self,
        key: tuple[object, object, object],
        value: object,
    ) -> None: ...

    def set_root_value(self, root_name: str, value: object) -> None: ...

    def set_module_global(
        self,
        file_path: Path,
        symbol_name: str,
        value: object,
    ) -> None: ...

    def set_workspace_attribute(self, attribute_name: str, value: object) -> None: ...


class Anchor(Protocol):
    """Protocol implemented by all resolved label anchors."""

    root_value: object
    field_parts: tuple[str, ...]
    entity_query: str | None

    def ensure_writable(self) -> None: ...

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None: ...

    def exposes_collection_view(self) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class BaseAnchor:
    """Default anchor behavior shared by all concrete anchor types."""

    root_value: object
    field_parts: tuple[str, ...] = ()
    entity_query: str | None = None

    def ensure_writable(self) -> None:
        """Validate that the anchor supports writeback."""

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None:
        """Persist an updated root back into the registry."""
        _ = (registry, updated_root)
        self.ensure_writable()

    def exposes_collection_view(self) -> bool:
        """Return True when the anchor resolves to an aggregate collection view."""
        return False


@dataclass(frozen=True, kw_only=True)
class WorkspaceAttributeAnchor(BaseAnchor):
    """Anchor for workspace virtual attributes such as ``'info.branch``."""

    root_attribute: str

    def ensure_writable(self) -> None:
        return None

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None:
        registry.set_workspace_attribute(self.root_attribute, updated_root)


@dataclass(frozen=True, kw_only=True)
class RegistryEntityAnchor(BaseAnchor):
    """Anchor for a registry-backed entity stored in ``evaluator.all``."""

    registry_key: tuple[object, object, object]

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None:
        registry.set_registry_entity(self.registry_key, updated_root)


@dataclass(frozen=True, kw_only=True)
class RootObjectAnchor(BaseAnchor):
    """Anchor for a whole registered root or a value reachable from it."""

    root_name: str

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None:
        registry.set_root_value(self.root_name, updated_root)


@dataclass(frozen=True, kw_only=True)
class ModuleGlobalAnchor(BaseAnchor):
    """Anchor for a top-level symbol in a lazily loaded module."""

    file_path: Path
    symbol_name: str

    def write_back(self, registry: RegistryWriter, updated_root: object) -> None:
        registry.set_module_global(self.file_path, self.symbol_name, updated_root)


@dataclass(frozen=True, kw_only=True)
class ModuleAggregateAnchor(BaseAnchor):
    """Read-only anchor for a module-level aggregate view."""

    root_name: str
    module_stem: str

    def ensure_writable(self) -> None:
        raise NotImplementedError("module aggregate assignments are not supported yet")

    def exposes_collection_view(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class RootCollectionAnchor(BaseAnchor):
    """Read-only anchor for the full root collection view."""

    def ensure_writable(self) -> None:
        raise NotImplementedError("root collection assignments are not supported yet")

    def exposes_collection_view(self) -> bool:
        return True
