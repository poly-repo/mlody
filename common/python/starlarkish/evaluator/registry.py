"""Registry containers for evaluator-managed entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

SUPPORTED_REGISTRATION_KINDS = (
    "root",
    "type",
    "location",
    "freshness",
    "representation",
    "value",
    "action",
    "task",
    "user",
    "implementation",
    "build_ref",
    "execution",
    "generic",
    "config",
)


class Named(Protocol):
    """A protocol for objects with a 'name' attribute."""

    name: str


@dataclass(slots=True)
class NamedRegistry:
    """Store evaluator entities by file-qualified key, name, and aggregate key."""

    kind: str
    _aggregate_sink: Callable[[object, Named], None]
    by_key: dict[str, Named] = field(default_factory=dict)
    by_name: dict[str, Named] = field(default_factory=dict)

    def register(self, key: str, thing: Named, *, replace: bool = False) -> None:
        existing_by_key = self.by_key.get(key)
        allow_value_shadow = self.kind == "value" and existing_by_key is not None
        if existing_by_key is not None and not replace and not allow_value_shadow:
            raise ValueError(
                f"Duplicate {self.kind} registration for key {key!r}: "
                f"{existing_by_key.name!r} is already registered."
            )

        self.by_key[key] = thing
        self.by_name[thing.name] = thing
        stem = key.rsplit(":", 1)[0] if ":" in key else None
        self._aggregate_sink((self.kind, stem, thing.name), thing)

    def fork(
        self,
        *,
        aggregate_sink: Callable[[object, Named], None],
        value_transform: Callable[[Named], Named] | None = None,
    ) -> NamedRegistry:
        transform = value_transform or (lambda item: item)
        return NamedRegistry(
            kind=self.kind,
            _aggregate_sink=aggregate_sink,
            by_key={key: transform(value) for key, value in self.by_key.items()},
            by_name={key: transform(value) for key, value in self.by_name.items()},
        )


@dataclass(slots=True)
class RegistryState:
    """Grouped evaluator registries keyed by entity kind."""

    all: dict[object, Named] = field(default_factory=dict)
    roots: NamedRegistry = field(init=False)
    types: NamedRegistry = field(init=False)
    locations: NamedRegistry = field(init=False)
    freshnesses: NamedRegistry = field(init=False)
    representations: NamedRegistry = field(init=False)
    values: NamedRegistry = field(init=False)
    actions: NamedRegistry = field(init=False)
    tasks: NamedRegistry = field(init=False)
    users: NamedRegistry = field(init=False)
    implementations: NamedRegistry = field(init=False)
    build_refs: NamedRegistry = field(init=False)
    executions: NamedRegistry = field(init=False)
    generics: NamedRegistry = field(init=False)
    configs: NamedRegistry = field(init=False)

    def __post_init__(self) -> None:
        self.roots = self._make_registry("root")
        self.types = self._make_registry("type")
        self.locations = self._make_registry("location")
        self.freshnesses = self._make_registry("freshness")
        self.representations = self._make_registry("representation")
        self.values = self._make_registry("value")
        self.actions = self._make_registry("action")
        self.tasks = self._make_registry("task")
        self.users = self._make_registry("user")
        self.implementations = self._make_registry("implementation")
        self.build_refs = self._make_registry("build_ref")
        self.executions = self._make_registry("execution")
        self.generics = self._make_registry("generic")
        self.configs = self._make_registry("config")

    def register(self, kind: str, key: str, thing: Named, *, replace: bool = False) -> None:
        self.for_kind(kind).register(key, thing, replace=replace)

    def _make_registry(self, kind: str) -> NamedRegistry:
        return NamedRegistry(kind=kind, _aggregate_sink=self._store_all)

    def _store_all(self, key: object, thing: Named) -> None:
        self.all[key] = thing

    def fork(
        self,
        *,
        value_transform: Callable[[Named], Named] | None = None,
    ) -> RegistryState:
        forked = RegistryState()
        transform = value_transform or (lambda item: item)
        clone_cache: dict[int, Named] = {}

        def _shared_transform(item: Named) -> Named:
            cached = clone_cache.get(id(item))
            if cached is not None:
                return cached
            cloned = transform(item)
            clone_cache[id(item)] = cloned
            return cloned

        for kind in SUPPORTED_REGISTRATION_KINDS:
            setattr(
                forked,
                f"{kind}s" if kind not in {"freshness", "build_ref"} else (
                    "freshnesses" if kind == "freshness" else "build_refs"
                ),
                self.for_kind(kind).fork(
                    aggregate_sink=forked._store_all,
                    value_transform=_shared_transform,
                ),
            )
        forked.all = {key: _shared_transform(value) for key, value in self.all.items()}
        return forked

    def for_kind(self, kind: str, *, operation: str = "registration") -> NamedRegistry:
        match kind:
            case "root":
                return self.roots
            case "type":
                return self.types
            case "location":
                return self.locations
            case "freshness":
                return self.freshnesses
            case "representation":
                return self.representations
            case "value":
                return self.values
            case "action":
                return self.actions
            case "task":
                return self.tasks
            case "user":
                return self.users
            case "implementation":
                return self.implementations
            case "build_ref":
                return self.build_refs
            case "execution":
                return self.executions
            case "generic":
                return self.generics
            case "config":
                return self.configs
            case _:
                supported = ", ".join(repr(item) for item in SUPPORTED_REGISTRATION_KINDS)
                if operation == "lookup":
                    raise ValueError(
                        f"Unknown lookup kind {kind!r}. Supported: {supported}."
                    )
                raise ValueError(
                    f"Unknown {operation} kind {kind!r}. Supported kinds: {supported}."
                )
