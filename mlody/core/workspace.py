"""Workspace: two-phase loading of .mlody pipeline definitions."""

from __future__ import annotations

import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    import networkx

from rich.console import Console

from mlody.common.struct import Struct, is_struct_like
from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.context import build_ctx
from mlody.core.anchor import (
    Anchor,
    ModuleAggregateAnchor,
    ModuleGlobalAnchor,
    RootCollectionAnchor,
    RootObjectAnchor,
    WorkspaceAttributeAnchor,
)
from mlody.core.registry_view import RegistryView
from mlody.core.source_parser import extract_entity_ranges
from mlody.core.targets import TargetAddress, parse_target, resolve_target_value
from mlody.core.traversal_runtime import step_named_child
from mlody.core.traversal_grammar import (
    IndexSegment,
    KeySegment,
    MlodySegment,
    SqlSegment,
    WildcardSegment,
)
from mlody.core.traversal_parser import TraversalParseError, parse_traversal_expression
from mlody.core.virtual_value import (
    force_virtual_value,
    is_virtual_value,
    make_virtual_value,
    traverse_virtual_value,
)
from mlody.core.workspace_loader import WorkspaceLoader
from mlody.core.workspace_models import RootInfo, WorkspaceLoadError

_logger = logging.getLogger(__name__)
_DEFAULT_SKIPPED_MLODY_PATHS = ("mlody/common/sandbox.mlody",)

# Resolver traversal hook — registered by mlody.resolver.label_value at import
# time.  workspace.py itself never imports from mlody.resolver; this breaks the
# core ↔ resolver BUILD dependency cycle.
_RESOLVER_TRAVERSE: Callable | None = None


def _register_resolver_traverse(fn: Callable) -> None:
    global _RESOLVER_TRAVERSE
    _RESOLVER_TRAVERSE = fn


def force(v: object) -> object:
    """Materialise a virtual value Struct; return all other inputs unchanged.

    A "virtual value" is a Struct with ``kind == "value"`` whose ``location``
    has ``type == "virtual"``.  In that case ``location.materializer(v)`` is
    called and its return value is returned.  All other inputs pass through.
    """
    return cast(Any, force_virtual_value(v))


def _clone_workspace_visible_value(value: object) -> object:
    if is_struct_like(value):
        changes: dict[str, object] = {}
        for name, child in value.as_mapping().items():
            cloned_child = _clone_workspace_visible_value(child)
            if cloned_child is not child:
                changes[str(name)] = cloned_child
        if not changes:
            return value
        return value.updated(**changes)
    if isinstance(value, dict):
        return {
            key: _clone_workspace_visible_value(child) for key, child in value.items()
        }
    if isinstance(value, list):
        return [_clone_workspace_visible_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_clone_workspace_visible_value(child) for child in value)
    if isinstance(value, set):
        return {_clone_workspace_visible_value(child) for child in value}
    if isinstance(value, frozenset):
        return frozenset(_clone_workspace_visible_value(child) for child in value)
    return value


class WorkspaceStateKind(str, Enum):
    LOADED = "loaded"
    BASELINE = "baseline"
    REQUEST = "request"


class Workspace:
    """Wraps the starlarkish Evaluator with two-phase loading and target resolution."""

    def __init__(
        self,
        monorepo_root: Path,
        roots_file: Path | None = None,
        full_workspace: bool = False,
        skipped_mlody_paths: tuple[str, ...] | list[str] | None = None,
        print_fn: Callable[..., None] = print,
        console: Console | None = None,
        extra_roots: dict[str, str] | None = None,
        lazy_roots: dict[str, str] | None = None,
        workspace_root: Path | None = None,
        state_kind: WorkspaceStateKind = WorkspaceStateKind.LOADED,
    ) -> None:
        self._monorepo_root = monorepo_root
        self._workspace_root = workspace_root if workspace_root is not None else monorepo_root
        self._roots_file = roots_file or (monorepo_root / "mlody" / "roots.mlody")
        self._full_workspace = full_workspace
        self._skipped_mlody_paths = tuple(
            skipped_mlody_paths
            if skipped_mlody_paths is not None
            else _DEFAULT_SKIPPED_MLODY_PATHS
        )
        self._console = console if console is not None else Console()
        self._evaluator = Evaluator(
            root=monorepo_root,
            print_fn=print_fn,
            extra_ctx=build_ctx(monorepo_root),
            line_range_extractor=extract_entity_ranges,
            resolve_hook=self._resolve_for_mlody,
            force_hook=force,
            setf_hook=self._setf_for_mlody,
        )
        self._registry = RegistryView(
            self._evaluator,
            workspace_attribute_writer=self._set_workspace_attribute,
        )
        self._root_infos: dict[str, RootInfo] = {}
        # extra_roots are eagerly globbed during Phase 2 (for example, a
        # sandbox-local @workspace root).
        self._extra_roots: dict[str, str] = extra_roots or {}
        # lazy_roots are available for on-demand resolution but are not
        # eagerly globbed (for example, @mlody for the full monorepo tree).
        self._lazy_roots: dict[str, str] = lazy_roots or {}
        self._workspace_attributes: dict[str, object] = {
            "info": self._build_workspace_info(),
        }
        self._dag_cache: "networkx.MultiDiGraph | None" = None
        self._state_kind = state_kind

    @property
    def evaluator(self) -> Evaluator:
        return self._evaluator

    @property
    def root_infos(self) -> dict[str, RootInfo]:
        return self._root_infos

    @property
    def registry_view(self) -> RegistryView:
        return self._registry

    @property
    def state_kind(self) -> WorkspaceStateKind:
        return self._state_kind

    @property
    def info(self) -> object:
        """Workspace-level metadata backed by workspace-owned mutable state."""
        return self.get_workspace_attribute("info")

    @property
    def dag(self) -> "networkx.MultiDiGraph":
        """Task dependency graph; built once and cached per workspace instance."""
        if self._dag_cache is None:
            from mlody.core.dag import build_dag  # noqa: PLC0415

            self._dag_cache = build_dag(self)
        return self._dag_cache

    def mark_baseline(self) -> Workspace:
        self._state_kind = WorkspaceStateKind.BASELINE
        return self

    def fork_request(self) -> Workspace:
        forked = Workspace(
            monorepo_root=self._monorepo_root,
            roots_file=self._roots_file,
            full_workspace=self._full_workspace,
            skipped_mlody_paths=self._skipped_mlody_paths,
            print_fn=self._evaluator._print_fn,
            console=self._console,
            extra_roots=dict(self._extra_roots),
            lazy_roots=dict(self._lazy_roots),
            workspace_root=(
                self._workspace_root
                if self._workspace_root != self._monorepo_root
                else None
            ),
            state_kind=WorkspaceStateKind.REQUEST,
        )
        forked._evaluator = self._evaluator.fork(
            resolve_hook=forked._resolve_for_mlody,
            force_hook=force,
            setf_hook=forked._setf_for_mlody,
        )
        forked._registry = RegistryView(
            forked._evaluator,
            workspace_attribute_writer=forked._set_workspace_attribute,
        )
        forked._root_infos = dict(self._root_infos)
        forked._workspace_attributes = {
            name: _clone_workspace_visible_value(value)
            for name, value in self._workspace_attributes.items()
        }
        forked._dag_cache = None
        return forked

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self._monorepo_root), *args],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _decorate_workspace_attribute(
        self,
        attribute_name: str,
        value: object,
    ) -> object:
        if not isinstance(value, Struct):
            return value
        descriptor_name = {
            "info": "mlody_workspace_info",
        }.get(attribute_name)
        if descriptor_name is None:
            return value
        descriptor = self._registry.type_by_name(descriptor_name)
        if descriptor is None or getattr(value, "_entity_type", None) is descriptor:
            return value
        return value.updated(_entity_type=descriptor)

    def _build_workspace_info(self) -> Struct:
        return cast(
            Struct,
            self._decorate_workspace_attribute(
                "info",
                Struct(
                    path=str(self._monorepo_root),
                    branch=self._git("branch", "--show-current"),
                    sha=self._git("rev-parse", "HEAD"),
                    roots=sorted(self._root_infos.keys()),
                ),
            ),
        )

    def _refresh_workspace_attributes(self) -> None:
        current_info = self._workspace_attributes.get("info")
        if isinstance(current_info, Struct):
            current_info = current_info.updated(roots=sorted(self._root_infos.keys()))
        else:
            current_info = self._build_workspace_info()
        self._workspace_attributes["info"] = self._decorate_workspace_attribute(
            "info",
            current_info,
        )

    def get_workspace_attribute(self, attribute_name: str) -> object:
        if attribute_name not in self._workspace_attributes:
            raise KeyError(f"workspace attribute {attribute_name!r} is not defined")
        return self._workspace_attributes[attribute_name]

    def _set_workspace_attribute(self, attribute_name: str, value: object) -> None:
        self._workspace_attributes[attribute_name] = self._decorate_workspace_attribute(
            attribute_name,
            value,
        )

    @staticmethod
    def _annotate_resolved_value(value: object, label: str) -> object:
        if is_virtual_value(value):
            return value
        if is_struct_like(value):
            return value.updated(_resolved_label=label)
        return value

    @staticmethod
    def _resolved_label_for_mlody_base(base: object) -> str | None:
        if isinstance(base, str):
            return base
        if is_virtual_value(base):
            label = getattr(base, "label", None)
            if isinstance(label, str):
                return label
        resolved_label = getattr(base, "_resolved_label", None)
        if isinstance(resolved_label, str):
            return resolved_label
        return None

    def _resolve_for_mlody(self, label: str) -> object:
        return self._annotate_resolved_value(self.resolve(label), label)

    def _setf_for_mlody(
        self,
        *,
        base: object,
        selector: object = "",
        value: object,
    ) -> object:
        from mlody.core.setf import setf as setf_label, setf_root

        selector_text = selector if isinstance(selector, str) else str(selector)
        resolved_label = self._resolved_label_for_mlody_base(base)
        if resolved_label is not None:
            target = resolved_label if selector_text == "" else f"{resolved_label}{selector_text}"
            setf_label(target, value, workspace=self)
            return self._resolve_for_mlody(resolved_label)
        return setf_root(base, selector, value)

    @staticmethod
    def _step_resolved_object(obj: object, segment: str) -> object:
        """Traverse one field while preserving list-by-name resolution."""
        return step_named_child(obj, segment)

    def resolve_label_anchor(self, target: str) -> Anchor:
        """Resolve a label string into a writable anchor plus residual path."""
        from mlody.core.label import parse_label as _core_parse_label

        lbl = _core_parse_label(target)

        if lbl.attribute_path is not None:
            root_attr = lbl.attribute_path[0]
            root_value = self.get_workspace_attribute(root_attr)
            return WorkspaceAttributeAnchor(
                root_value=root_value,
                root_attribute=root_attr,
                field_parts=lbl.attribute_path[1:],
            )

        if lbl.entity is None:
            msg = f"Label {target!r} does not select a writable anchor"
            raise ValueError(msg)

        registry_anchor = self._registry.match_registry_entity_label(
            target,
            entity=lbl.entity,
            entity_query=lbl.entity_query,
            attribute_path=lbl.attribute_path,
            root_infos=self._root_infos,
        )
        if registry_anchor is not None:
            return registry_anchor

        entity = lbl.entity
        name = entity.name
        if name is not None and entity.root is not None and self._registry.has_root(entity.root):
            name_parts = name.split(".")
            field_parts = entity.field_path or tuple(name_parts[1:])
            if lbl.attribute_path:
                field_parts = field_parts + lbl.attribute_path
            return RootObjectAnchor(
                root_value=self._registry.root_value(entity.root),
                root_name=entity.root,
                field_parts=(name_parts[0],) + field_parts,
                entity_query=lbl.entity_query,
            )

        if name is not None and entity.root is None:
            file_path = self._monorepo_root / (entity.path.lstrip("/") + ".mlody")
            self._registry.ensure_module_loaded(file_path)
            module_globals = self._registry.module_globals(file_path)
            name_parts = name.split(".")
            if name_parts[0] not in module_globals:
                raise KeyError(f"Entity {name_parts[0]!r} not found in {file_path}")
            field_parts = entity.field_path or tuple(name_parts[1:])
            if lbl.attribute_path:
                field_parts = field_parts + lbl.attribute_path
            return ModuleGlobalAnchor(
                root_value=module_globals[name_parts[0]],
                file_path=file_path,
                symbol_name=name_parts[0],
                field_parts=field_parts,
                entity_query=lbl.entity_query,
            )

        if entity.root is not None:
            if not self._registry.has_root(entity.root):
                available = list(self._registry.available_root_names())
                msg = f"Root {entity.root!r} not found; available roots: {available}"
                raise KeyError(msg)
            if entity.path and entity.root in self._root_infos:
                stem_parts_mod: list[str] = []
                root_rel_mod = self._root_infos[entity.root].path.lstrip("/").rstrip("/")
                if root_rel_mod:
                    stem_parts_mod.append(root_rel_mod)
                stem_parts_mod.append(entity.path.lstrip("/").rstrip("/"))
                mod_stem = "/".join([part for part in stem_parts_mod if part])
                return ModuleAggregateAnchor(
                    root_value=self._registry.module_aggregate(mod_stem),
                    root_name=entity.root,
                    module_stem=mod_stem,
                )
            return RootObjectAnchor(
                root_value=self._registry.root_value(entity.root),
                root_name=entity.root,
            )

        return RootCollectionAnchor(
            root_value=self._registry.root_values_snapshot(),
        )

    @staticmethod
    def _convert_single_entity(entity: object) -> object:
        """Convert ``inputs``, ``outputs``, and ``config`` port lists to named mappings.

        Returns a new entity with those three fields replaced by ``Struct`` or
        ``dict`` objects keyed by element ``name``. All other fields are
        preserved unchanged.

        Idempotent: if a field is already a named ``Struct`` or ``dict`` it is
        left as-is.
        Raises ``ValueError`` if any element lacks a ``name`` or if duplicate
        names appear within the same list.
        """
        # Recursively convert an embedded action entity before reconstructing
        # the outer entity, so that task.action.outputs.X traversal works.
        action_field = getattr(entity, "action", None)
        if (
            is_struct_like(action_field)
            and getattr(action_field, "kind", None) == "action"
        ):
            action_field = Workspace._convert_single_entity(action_field)

        entity_kind = getattr(entity, "kind", "<unknown>")
        entity_name = getattr(entity, "name", "<unknown>")

        def _convert_port(field_name: str) -> dict[str, object]:
            lst: object = getattr(entity, field_name, None)
            # Idempotency: already a name-keyed dict — leave it unchanged.
            if isinstance(lst, dict):
                return lst
            # Treat None, empty list, or legacy Struct as an empty dict.
            if not lst:
                return {}
            # Struct from an older normalization pass — convert to dict.
            if isinstance(lst, Struct):
                return dict(lst.as_mapping())
            # lst is a non-empty list; validate and build the named dict.
            seen: dict[str, int] = {}
            for idx, el in enumerate(lst):  # type: ignore[union-attr]
                name = getattr(el, "name", None)
                if not name:
                    msg = (
                        f"Entity {entity_kind!r}/{entity_name!r}: "
                        f"element at index {idx} of field {field_name!r} "
                        f"is missing a non-empty 'name' field"
                    )
                    raise ValueError(msg)
                if name in seen:
                    msg = (
                        f"Entity {entity_kind!r}/{entity_name!r}: "
                        f"duplicate name {name!r} in field {field_name!r} "
                        f"(first at index {seen[name]}, repeated at index {idx})"
                    )
                    raise ValueError(msg)
                seen[name] = idx
            return {el.name: el for el in lst}  # type: ignore[union-attr]

        new_inputs = _convert_port("inputs")
        new_outputs = _convert_port("outputs")
        new_config = _convert_port("config")

        updated: dict[str, object] = {
            "inputs": new_inputs,
            "outputs": new_outputs,
            "config": new_config,
        }
        if action_field is not None:
            updated["action"] = action_field
        return entity.updated(**updated)

    def _convert_ports_to_structs(self) -> None:
        """Replace port lists on every task/action entity in the evaluator registry.

        Iterates the shared registry view, converts each ``task`` and
        ``action`` entity via ``_convert_single_entity``, and stages the
        updates before writing them back.
        """
        staging: dict[object, object] = {}
        for key, value in self._registry.iter_registry_items():
            if not is_struct_like(value):
                continue
            if getattr(value, "kind", None) not in ("task", "action"):
                continue
            staging[key] = self._convert_single_entity(value)
        for key, new_value in staging.items():
            self._registry.set_registry_entity(key, new_value)

    def _resolve_value_sources(self) -> None:
        """Resolve string and raw-Struct sources to dataclass references.

        Runs after _convert_ports_to_structs so all port collections are already
        dict[str, RegisteredValue]. Value labels resolve to RegisteredValue
        instances; task-path labels resolve to PortRef dataclasses. Uses
        object.__setattr__ to mutate frozen RegisteredValue dataclasses in place.
        """
        from mlody.common.action import RegisteredAction  # noqa: PLC0415
        from mlody.common.task import RegisteredTask  # noqa: PLC0415
        from mlody.common.value import RegisteredValue  # noqa: PLC0415
        from mlody.core.dag import (  # noqa: PLC0415
            PortLocationParseError,
            PortRef,
            parse_port_location,
        )

        task_outputs: dict[str, dict[str, RegisteredValue]] = {}
        standalone_values: dict[str, RegisteredValue] = {}

        for _key, entity in self._registry.iter_registry_items():
            if isinstance(entity, RegisteredTask):
                task_outputs[entity.name] = dict(entity.outputs)
            elif isinstance(entity, RegisteredValue):
                standalone_values[entity.name] = entity

        def _resolve_one(rv: RegisteredValue) -> None:
            src = rv.source
            if src is None or isinstance(src, (RegisteredValue, PortRef)):
                return

            resolved: RegisteredValue | PortRef | None = None

            if isinstance(src, str) and src.startswith(":"):
                after = src[1:]
                if "." in after:
                    try:
                        resolved = parse_port_location(src)
                    except PortLocationParseError:
                        resolved = None
                else:
                    for ports in task_outputs.values():
                        if after in ports:
                            resolved = ports[after]
                            break
                    if resolved is None:
                        resolved = standalone_values.get(after)

            elif is_struct_like(src) and getattr(src, "kind", None) == "value":
                try:
                    resolved = RegisteredValue(src)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    pass

            if resolved is not None:
                object.__setattr__(rv, "source", resolved)

        for _key, entity in self._registry.iter_registry_items():
            if isinstance(entity, RegisteredValue):
                _resolve_one(entity)
            elif isinstance(entity, RegisteredTask):
                for rv in (
                    *entity.inputs.values(),
                    *entity.outputs.values(),
                    *entity.config.values(),
                ):
                    _resolve_one(rv)
                action = entity.action
                if isinstance(action, RegisteredAction):
                    for rv in (
                        *action.inputs.values(),
                        *action.outputs.values(),
                        *action.config.values(),
                    ):
                        _resolve_one(rv)

    def _is_skipped_mlody_file(self, mlody_file: Path) -> bool:
        """Return True when a file matches the configured skip patterns.

        Pattern rules:
        - `path/to/file.mlody` skips exactly that file.
        - `path/...` skips all files under `path/`.
        """
        rel = mlody_file.relative_to(self._monorepo_root).as_posix()
        for raw_pattern in self._skipped_mlody_paths:
            pattern = raw_pattern.strip().lstrip("./").lstrip("/")
            if not pattern:
                continue
            if pattern.endswith("/..."):
                prefix = pattern[:-4].rstrip("/")
                if not prefix:
                    return True
                if rel.startswith(f"{prefix}/"):
                    return True
                continue
            if rel == pattern:
                return True
        return False

    def load(self, verbose: bool = False) -> None:
        """Execute two-phase loading of pipeline definitions."""
        # Keep the verbose parameter for API compatibility. The CLI's
        # --verbose flag now controls logging level only; RegistryView.debug_dump()
        # remains available for a future explicit dump attribute.
        _ = verbose
        loader = WorkspaceLoader(
            monorepo_root=self._monorepo_root,
            workspace_root=self._workspace_root,
            roots_file=self._roots_file,
            root_infos=self._root_infos,
            registry=self._registry,
            extra_roots=self._extra_roots,
            lazy_roots=self._lazy_roots,
            should_skip_mlody_file=(
                (lambda _path: False)
                if self._full_workspace
                else self._is_skipped_mlody_file
            ),
            convert_ports_to_structs=self._convert_ports_to_structs,
            resolve_value_sources=self._resolve_value_sources,
            after_root_discovery=self._refresh_workspace_attributes,
        )
        loader.load(workspace=self)
        self._refresh_workspace_attributes()

    def resolve(self, target: str | TargetAddress) -> object:
        """Parse (if string) and resolve a target to a value.

        Supports:
        - Entity-spec labels with a name:  @root//pkg:name, //pkg:name, :name
        - Entity-spec labels without name, no path: @root  → root struct
        - Entity-spec labels without name, with path: @root//pkg/module → dict of all
          entities registered from that module, keyed by ``"kind/name"``
        - Workspace-level attribute labels: 'attr, 'attr.subfield
        """
        if isinstance(target, str) and target.startswith("'"):
            # Workspace-attribute label: return a virtual value Struct whose
            # materializer forces the attribute access lazily.
            from mlody.core.label import parse_label as _core_parse_label

            lbl = _core_parse_label(target)
            if lbl.attribute_path is None:
                msg = f"Empty attribute path in label: {target!r}"
                raise ValueError(msg)

            ws_type = self._registry.type_by_name("mlody-workspace")
            if ws_type is None:
                msg = (
                    "Type 'mlody-workspace' is not registered; ensure load() "
                    "is called before resolve()"
                )
                raise RuntimeError(msg)

            def _workspace_materializer(_v: object) -> object:
                _logger.debug(
                    "workspace materializer invoked for label %r with value %r",
                    target,
                    _v,
                )
                return self

            root_value = make_virtual_value(
                value_type=ws_type,
                label=target,
                materializer=_workspace_materializer,
            )
            return traverse_virtual_value(root_value, lbl.attribute_path, target)

        if isinstance(target, str) and (
            target.startswith("@") or target.startswith("//")
        ):
            # Use the core label parser to detect name-less entity specs
            # (e.g. @root//path or //path without a :name).  parse_target
            # requires a :name, so we handle this case directly.
            from mlody.core.label import parse_label as _core_parse_label
            from mlody.core.label.errors import LabelParseError as _LabelParseError

            try:
                lbl = _core_parse_label(target)
            except _LabelParseError:
                pass  # fall through to parse_target for legacy error handling
            else:
                if lbl.entity is not None:
                    anchor = self.resolve_label_anchor(target)
                    obj = anchor.root_value
                    field_parts = anchor.field_parts
                    eq = anchor.entity_query
                    expr2 = None
                    seg2 = None
                    if eq is not None:
                        try:
                            expr2 = parse_traversal_expression(f"[{eq}]")
                        except TraversalParseError:
                            expr2 = None
                        if expr2 is not None and expr2.segments:
                            seg2 = expr2.segments[0]

                    if (
                        anchor.exposes_collection_view()
                        and not field_parts
                        and anchor.entity_query is None
                    ):
                        return obj

                    # Resolve direct entity labels (with optional dotted field path)
                    # against evaluator registrations by (stem, name), where:
                    #   stem = "<root_path>/<entity.path>" for @root labels
                    #   stem = "<entity.path>" for // labels
                    # Record-traversal branch: activates when the resolved
                    # base value is a record-typed value struct with one or
                    # more field-path segments.  Uses _traverse_one_step for
                    # each segment so location composition is applied at
                    # every level (OQ-12 deduplication).
                    obj_type = getattr(obj, "type", None)
                    _is_record_type = (
                        getattr(obj_type, "kind", None) == "record"
                        or getattr(obj_type, "_root_kind", None) == "record"
                    )
                    if (
                        len(field_parts) >= 1
                        and getattr(obj, "kind", None) == "value"
                        and _is_record_type
                        and _RESOLVER_TRAVERSE is not None
                    ):
                        return _RESOLVER_TRAVERSE(
                            obj, field_parts, anchor.entity_query, lbl
                        )
                    if isinstance(seg2, SqlSegment) and _RESOLVER_TRAVERSE is not None:
                        return _RESOLVER_TRAVERSE(
                            obj, field_parts, anchor.entity_query, lbl
                        )

                    for field in field_parts:
                        obj = self._step_resolved_object(obj, field)

                    if seg2 is not None:
                        if isinstance(seg2, IndexSegment) and isinstance(
                            obj,
                            (list, tuple),
                        ):
                            obj = obj[seg2.index]
                        elif isinstance(seg2, KeySegment) and isinstance(obj, dict):
                            obj = obj[seg2.key]
                        elif isinstance(seg2, WildcardSegment) and isinstance(
                            obj,
                            (list, tuple, dict),
                        ):
                            obj = (
                                list(obj.values())
                                if isinstance(obj, dict)
                                else list(obj)
                            )
                    return obj

        address = parse_target(target) if isinstance(target, str) else target
        return resolve_target_value(address, self._registry.root_mapping())

    def expand_wildcard_label(self, inner_label: str) -> list[str]:
        """Expand a wildcard inner label into one or more concrete labels."""
        return self._registry.expand_wildcard_label(
            inner_label,
            root_infos=self._root_infos,
        )
