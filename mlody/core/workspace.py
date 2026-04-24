"""Workspace: two-phase loading of .mlody pipeline definitions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from rich.console import Console
from rich.syntax import Syntax

from common.python.starlarkish.core.struct import Struct
from common.python.starlarkish.evaluator.evaluator import Evaluator
from mlody.common.context import build_ctx
from mlody.core.source_parser import extract_entity_ranges
from mlody.core.targets import TargetAddress, parse_target, resolve_target_value
from mlody.core.virtual_value import force_virtual_value, make_virtual_value, traverse_virtual_value

_logger = logging.getLogger(__name__)
_DEFAULT_SKIPPED_MLODY_PATHS = ("mlody/common/sandbox.mlody",)


def force(v: object) -> object:
    """Materialise a virtual value Struct; return all other inputs unchanged.

    A "virtual value" is a Struct with ``kind == "value"`` whose ``location``
    has ``type == "virtual"``.  In that case ``location.materializer(v)`` is
    called and its return value is returned.  All other inputs pass through.
    """
    return cast(Any, force_virtual_value(v))


class WorkspaceLoadError(Exception):
    """One or more .mlody files failed to evaluate during Phase 2 loading."""

    def __init__(self, failures: list[tuple[Path, Exception]]) -> None:
        self.failures = failures
        lines = "\n".join(
            f"  {path}: {type(exc).__name__}: {exc}"
            for path, exc in failures
        )
        super().__init__(f"{len(failures)} file(s) failed to load:\n{lines}")


@dataclass(frozen=True)
class RootInfo:
    """Metadata for a registered root."""

    name: str
    path: str
    description: str


@dataclass(frozen=True)
class LabelWriteAnchor:
    """Writable anchor for a resolved label target."""

    root_value: object
    writeback_kind: str
    writeback_locator: object | None
    field_parts: tuple[str, ...] = ()
    entity_query: str | None = None


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
        )
        self._root_infos: dict[str, RootInfo] = {}
        # extra_roots: registered in both _root_infos and _roots_by_name; Phase 2
        # eagerly globs their directories (e.g. @workspace pointing to the sandbox).
        self._extra_roots: dict[str, str] = extra_roots or {}
        # lazy_roots: registered only in _roots_by_name for on-demand load() resolution
        # (e.g. @mlody pointing to the monorepo mlody/ dir — too large to pre-glob).
        self._lazy_roots: dict[str, str] = lazy_roots or {}

    @property
    def evaluator(self) -> Evaluator:
        return self._evaluator

    @property
    def root_infos(self) -> dict[str, RootInfo]:
        return self._root_infos

    @property
    def info(self) -> object:
        """Synthesised workspace-level metadata (git state + registered roots).

        Returned as a Struct so field access works in .mlody files and the
        show command can traverse sub-fields (e.g. "'info.branch").
        """
        import subprocess

        from common.python.starlarkish.core.struct import struct

        def _git(*args: str) -> str:
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

        return struct(
            path=str(self._monorepo_root),
            branch=_git("branch", "--show-current"),
            sha=_git("rev-parse", "HEAD"),
            roots=sorted(self._root_infos.keys()),
        )

    @staticmethod
    def _step_resolved_object(obj: object, segment: str) -> object:
        """Traverse one field while preserving list-by-name resolution."""
        if isinstance(obj, list):
            for item in obj:
                if getattr(item, "name", None) == segment:
                    return item
            raise KeyError(segment)
        return getattr(obj, segment)

    def _match_registry_entity_label(
        self,
        target: str,
        *,
        entity: object,
        attribute_path: tuple[str, ...] | None,
    ) -> LabelWriteAnchor | None:
        """Return a registry-backed label anchor when the entity is registered."""
        entity_root = getattr(entity, "root", None)
        entity_path = getattr(entity, "path", None)
        entity_name = getattr(entity, "name", None)
        entity_field_path = getattr(entity, "field_path", ()) or ()
        entity_query = getattr(entity, "entity_query", None)

        if entity_name is None:
            return None

        name_parts = entity_name.split(".")
        base_name = name_parts[0]
        if entity_field_path:
            field_parts = entity_field_path
        else:
            field_parts = tuple(name_parts[1:])
        if attribute_path:
            field_parts = field_parts + attribute_path

        stem_parts: list[str] = []
        can_registry_resolve = True
        if entity_root is not None:
            if entity_root in self._root_infos:
                root_rel = self._root_infos[entity_root].path.lstrip("/").rstrip("/")
                if root_rel:
                    stem_parts.append(root_rel)
            elif entity_root in self._evaluator._roots_by_name:
                can_registry_resolve = False
            else:
                available = sorted(self._evaluator._roots_by_name)
                msg = f"Root {entity_root!r} not found; available roots: {available}"
                raise KeyError(msg)
        if entity_path:
            stem_parts.append(entity_path.lstrip("/").rstrip("/"))
        stem = "/".join([part for part in stem_parts if part])
        path_suffix = entity_path.lstrip("/").rstrip("/") if entity_path else ""
        root_prefix = None
        if entity_root is not None and entity_root in self._root_infos:
            root_prefix = self._root_infos[entity_root].path.lstrip("/").rstrip("/")

        matches: list[tuple[tuple[object, object, object], object]] = []
        if can_registry_resolve:
            for key, value in self._evaluator.all.items():
                if (
                    isinstance(key, tuple)
                    and len(key) == 3
                    and key[1] == stem
                    and key[2] == base_name
                ):
                    matches.append((key, value))

        if can_registry_resolve and not matches:
            for key, value in self._evaluator.all.items():
                if not (isinstance(key, tuple) and len(key) == 3 and key[2] == base_name):
                    continue
                key_stem = key[1]
                if not isinstance(key_stem, str):
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
        matches.sort(key=lambda kv: kind_order.get(kv[0][0], 99))
        match_key, match_value = matches[0]
        return LabelWriteAnchor(
            root_value=match_value,
            writeback_kind="registry_entity",
            writeback_locator=match_key,
            field_parts=field_parts,
            entity_query=entity_query,
        )

    def resolve_label_anchor(self, target: str) -> LabelWriteAnchor:
        """Resolve a label string into a writable anchor plus residual path."""
        from mlody.core.label import parse_label as _core_parse_label

        lbl = _core_parse_label(target)

        if lbl.attribute_path is not None:
            root_attr = lbl.attribute_path[0]
            root_value = self.resolve(f"'{root_attr}")
            return LabelWriteAnchor(
                root_value=root_value,
                writeback_kind="workspace_attribute",
                writeback_locator=root_attr,
                field_parts=lbl.attribute_path[1:],
            )

        if lbl.entity is None:
            msg = f"Label {target!r} does not select a writable anchor"
            raise ValueError(msg)

        registry_anchor = self._match_registry_entity_label(
            target,
            entity=lbl.entity,
            attribute_path=lbl.attribute_path,
        )
        if registry_anchor is not None:
            return registry_anchor

        entity = lbl.entity
        name = entity.name
        if name is not None and entity.root is not None and entity.root in self._evaluator._roots_by_name:
            name_parts = name.split(".")
            field_parts = entity.field_path or tuple(name_parts[1:])
            if lbl.attribute_path:
                field_parts = field_parts + lbl.attribute_path
            return LabelWriteAnchor(
                root_value=self._evaluator._roots_by_name[entity.root],
                writeback_kind="root_object",
                writeback_locator=entity.root,
                field_parts=(name_parts[0],) + field_parts,
                entity_query=lbl.entity_query,
            )

        if name is not None and entity.root is None:
            file_path = self._monorepo_root / (entity.path.lstrip("/") + ".mlody")
            if file_path not in self._evaluator.loaded_files:
                self._evaluator.eval_file(file_path)
            module_globals: dict[str, object] = self._evaluator._module_globals.get(file_path, {})  # type: ignore[attr-defined]
            name_parts = name.split(".")
            if name_parts[0] not in module_globals:
                raise KeyError(f"Entity {name_parts[0]!r} not found in {file_path}")
            field_parts = entity.field_path or tuple(name_parts[1:])
            if lbl.attribute_path:
                field_parts = field_parts + lbl.attribute_path
            return LabelWriteAnchor(
                root_value=module_globals[name_parts[0]],
                writeback_kind="module_global",
                writeback_locator=(file_path, name_parts[0]),
                field_parts=field_parts,
                entity_query=lbl.entity_query,
            )

        roots = self._evaluator._roots_by_name
        if entity.root is not None:
            if entity.root not in roots:
                available = sorted(roots)
                msg = f"Root {entity.root!r} not found; available roots: {available}"
                raise KeyError(msg)
            if entity.path and entity.root in self._root_infos:
                stem_parts_mod: list[str] = []
                root_rel_mod = self._root_infos[entity.root].path.lstrip("/").rstrip("/")
                if root_rel_mod:
                    stem_parts_mod.append(root_rel_mod)
                stem_parts_mod.append(entity.path.lstrip("/").rstrip("/"))
                mod_stem = "/".join([part for part in stem_parts_mod if part])
                module_value = {
                    f"{key[0]}/{key[2]}": value
                    for key, value in self._evaluator.all.items()
                    if isinstance(key, tuple) and len(key) == 3 and key[1] == mod_stem
                }
                return LabelWriteAnchor(
                    root_value=module_value,
                    writeback_kind="module_aggregate",
                    writeback_locator=(entity.root, mod_stem),
                )
            return LabelWriteAnchor(
                root_value=roots[entity.root],
                writeback_kind="root_object",
                writeback_locator=entity.root,
            )

        return LabelWriteAnchor(
            root_value=dict(roots),
            writeback_kind="root_collection",
            writeback_locator=None,
        )

    @staticmethod
    def _convert_single_entity(entity: Struct) -> Struct:
        """Convert ``inputs``, ``outputs``, and ``config`` port lists to named Structs.

        Returns a new ``Struct`` with those three fields replaced by ``Struct``
        objects keyed by element ``name``.  All other fields are preserved
        unchanged.

        Idempotent: if a field is already a ``Struct`` it is left as-is.
        Raises ``ValueError`` if any element lacks a ``name`` or if duplicate
        names appear within the same list.
        """
        # Recursively convert an embedded action Struct before reconstructing
        # the outer entity, so that task.action.outputs.X traversal works.
        action_field = getattr(entity, "action", None)
        if (
            isinstance(action_field, Struct)
            and getattr(action_field, "kind", None) == "action"
        ):
            action_field = Workspace._convert_single_entity(action_field)

        entity_kind = getattr(entity, "kind", "<unknown>")
        entity_name = getattr(entity, "name", "<unknown>")

        def _convert_port(field_name: str) -> Struct:
            lst: object = getattr(entity, field_name, None)
            # Idempotency: already a Struct — leave it unchanged.
            if isinstance(lst, Struct):
                return lst
            # Treat None or empty list as an empty Struct.
            if not lst:
                return Struct()
            # lst is a non-empty list; validate and build the named Struct.
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
            return Struct(**{el.name: el for el in lst})  # type: ignore[union-attr]

        new_inputs = _convert_port("inputs")
        new_outputs = _convert_port("outputs")
        new_config = _convert_port("config")

        updated: dict[str, object] = {
            **entity._fields,
            "inputs": new_inputs,
            "outputs": new_outputs,
            "config": new_config,
        }
        if action_field is not None:
            updated["action"] = action_field
        return Struct(**updated)

    def _convert_ports_to_structs(self) -> None:
        """Replace port lists on every task/action entity in the evaluator registry.

        Iterates ``self._evaluator.all``, converts each ``task`` and ``action``
        entity via ``_convert_single_entity``, and writes the results back.
        Updates are staged in a temporary dict to avoid mutating the dict
        during iteration.
        """
        staging: dict[object, Struct] = {}
        for key, value in self._evaluator.all.items():
            if not isinstance(value, Struct):
                continue
            if getattr(value, "kind", None) not in ("task", "action"):
                continue
            staging[key] = self._convert_single_entity(value)
        for key, new_value in staging.items():
            self._evaluator.all[key] = new_value  # type: ignore[index]

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
        # Phase 1: Root discovery.  When no roots file exists (e.g. a sandbox
        # workspace addressed via --workspace that has no roots.mlody), Phase 1
        # is skipped and the workspace operates purely from injected roots.
        if self._roots_file.exists():
            self._evaluator.eval_file(self._roots_file)

        # Load type definitions (best-effort; may not be available in all environments)
        types_path = self._monorepo_root / "mlody" / "common" / "types.mlody"
        if types_path not in self._evaluator.loaded_files:
            try:
                self._evaluator.eval_file(types_path)
            except Exception:
                pass

        self._root_infos = {}
        for _key, root_obj in self._evaluator.roots.items():
            name = root_obj.name
            self._root_infos[name] = RootInfo(
                name=name,
                path=getattr(root_obj, "path", ""),
                description=getattr(root_obj, "description", ""),
            )

        # Extra roots: added to _root_infos so Phase 2 eagerly globs their files
        # (e.g. @workspace → the sandbox directory itself).
        for root_name, root_path in self._extra_roots.items():
            if root_name not in self._root_infos:
                self._root_infos[root_name] = RootInfo(
                    name=root_name,
                    path=root_path,
                    description="injected",
                )
                self._evaluator._roots_by_name[root_name] = Struct(
                    name=root_name,
                    path=root_path,
                    description="injected",
                )

        # Lazy roots: only in _roots_by_name for on-demand load() resolution;
        # not pre-globbed (e.g. @mlody — the full mlody tree is too large to
        # load eagerly into a sandbox workspace).
        for root_name, root_path in self._lazy_roots.items():
            if root_name not in self._evaluator._roots_by_name:
                self._evaluator._roots_by_name[root_name] = Struct(
                    name=root_name,
                    path=root_path,
                    description="injected",
                )

        # Phase 2: Full evaluation
        # .resolve() normalises any ".." or "." components in injected root paths.
        load_errors: list[tuple[Path, Exception]] = []
        for info in self._root_infos.values():
            root_abs = (self._monorepo_root / info.path.lstrip("/")).resolve()
            _logger.debug("Loading root: %s", root_abs)
            if not root_abs.is_dir():
                continue
            for mlody_file in sorted(root_abs.glob("**/*.mlody")):
                if not self._full_workspace and self._is_skipped_mlody_file(mlody_file):
                    _logger.debug("Skipping %s due to workspace skip list", mlody_file)
                    continue
                if mlody_file in self._evaluator.loaded_files:
                    continue
                try:
                    self._evaluator.eval_file(mlody_file)
                except Exception as exc:
                    _logger.error(
                        "Failed to load %s: %s: %s", mlody_file, type(exc).__name__, exc
                    )
                    load_errors.append((mlody_file, exc))

        if load_errors:
            raise WorkspaceLoadError(load_errors)

        self._evaluator.resolve()

        # Phase 3: Convert port lists (inputs/outputs/config) on task and action
        # entities to named Structs, enabling pure getattr-based traversal.
        self._convert_ports_to_structs()

        if verbose:
            data = {str(k): v.to_dict() if hasattr(v, "to_dict") else v for k, v in self._evaluator.all.items()}
            self._console.print(Syntax(json.dumps(data, indent=2, default=repr), "json"))

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

            ws_type = self._evaluator._types_by_name.get("mlody-workspace")  # type: ignore[attr-defined]
            if ws_type is None:
                msg = "Type 'mlody-workspace' is not registered; ensure load() is called before resolve()"
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

                    if anchor.writeback_kind in {
                        "module_aggregate",
                        "root_collection",
                    } and not field_parts and anchor.entity_query is None:
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
                    ):
                        from mlody.resolver.label_value import (  # noqa: PLC0415
                            MlodyUnresolvedValue as _MlodyUnresolvedValue,
                            TraversalErrorPolicy as _TraversalErrorPolicy,
                            _RawAttrValue as _RawAttrValue_t,
                            _traverse_one_step as _ts,
                        )
                        from mlody.core.traversal_parser import (  # noqa: PLC0415
                            TraversalParseError as _TraversalParseError,
                            parse_traversal_expression as _parse_traversal,
                        )

                        current: object = obj
                        for fp_i, fp_seg in enumerate(field_parts):
                            step_result = _ts(
                                current, fp_seg, tuple(field_parts[:fp_i]), lbl
                            )
                            if isinstance(step_result, _MlodyUnresolvedValue):
                                return step_result
                            if isinstance(step_result, tuple):
                                current = step_result[0]
                            else:
                                current = step_result
                                break

                        eq = anchor.entity_query
                        if eq is not None:
                            try:
                                expr = _parse_traversal(f"[{eq}]")
                            except _TraversalParseError:
                                expr = None
                            if expr is not None and expr.segments:
                                seg = expr.segments[0]
                                q_result = _ts(
                                    current,
                                    seg,
                                    field_parts,
                                    lbl,
                                    _TraversalErrorPolicy.RAISE,
                                )
                                if isinstance(q_result, _MlodyUnresolvedValue):
                                    return q_result
                                if isinstance(q_result, tuple):
                                    current = q_result[0]
                                else:
                                    return getattr(q_result, "value", q_result)

                        if isinstance(current, _RawAttrValue_t):
                            return current.value
                        return current

                    for field in field_parts:
                        obj = self._step_resolved_object(obj, field)

                    eq = anchor.entity_query
                    if eq is not None:
                        from mlody.core.traversal_parser import (  # noqa: PLC0415
                            TraversalParseError as _TraversalParseError2,
                            parse_traversal_expression as _parse_traversal2,
                        )
                        try:
                            expr2 = _parse_traversal2(f"[{eq}]")
                        except _TraversalParseError2:
                            expr2 = None
                        if expr2 is not None and expr2.segments:
                            from mlody.core.traversal_grammar import (  # noqa: PLC0415
                                IndexSegment as _IndexSegment,
                                KeySegment as _KeySegment,
                                WildcardSegment as _WildcardSegment,
                            )
                            seg2 = expr2.segments[0]
                            if isinstance(seg2, _IndexSegment) and isinstance(obj, (list, tuple)):
                                obj = obj[seg2.index]
                            elif isinstance(seg2, _KeySegment) and isinstance(obj, dict):
                                obj = obj[seg2.key]
                            elif isinstance(seg2, _WildcardSegment) and isinstance(obj, (list, tuple, dict)):
                                obj = list(obj.values()) if isinstance(obj, dict) else list(obj)
                    return obj

        address = parse_target(target) if isinstance(target, str) else target
        return resolve_target_value(address, self._evaluator._roots_by_name)

    def expand_wildcard_label(self, inner_label: str) -> list[str]:
        """Expand a wildcard inner label into concrete labels (wildcard=False).

        Scans the loaded evaluator registry for all stems matching the wildcard
        pattern and returns one concrete label string per matching stem.

        If the label is not a wildcard, returns ``[inner_label]`` unchanged.
        """
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
        if entity.root is not None and entity.root in self._root_infos:
            root_prefix = self._root_infos[entity.root].path.lstrip("/").rstrip("/")

        path_suffix = entity.path.lstrip("/").rstrip("/") if entity.path else ""

        stems: set[str] = set()
        for key in self._evaluator.all:
            if not (isinstance(key, tuple) and len(key) == 3):
                continue
            k_stem, k_name = key[1], key[2]
            if not isinstance(k_stem, str):
                continue
            if base_name is not None and k_name != base_name:
                continue
            if root_prefix is not None and not k_stem.startswith(root_prefix):
                continue
            if path_suffix and not k_stem.endswith(path_suffix):
                continue
            stems.add(k_stem)

        result: list[str] = []
        for stem in sorted(stems):
            if root_prefix and stem.startswith(root_prefix):
                rel_path = stem[len(root_prefix):].lstrip("/")
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
                parts.append(f"'{'.' .join(lbl.attribute_path)}")
            result.append("".join(parts))

        return result
