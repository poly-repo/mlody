"""Public API for selector-based assignment in mlody."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mlody.common.struct import Struct, is_struct_like

from mlody.core.anchor import Anchor
from mlody.core.label import parse_label as parse_ref_label
from mlody.core.lineage import append_lineage, build_lineage_event
from mlody.core.place import AssignmentMode, MISSING_PLACE_VALUE, Place, PlaceSet
from mlody.core.setf_strategies import (
    DictKeySetter,
    ListIndexSetter,
    ReadOnlyFieldSetter,
    SequenceSliceSetter,
    StructFieldSetter,
    VirtualValueFieldSetter,
)
from mlody.core.traversal_runtime import (
    has_named_child,
    iter_children,
    replace_child,
    step_segment,
)
from mlody.core.traversal_grammar import (
    FieldSegment,
    IndexSegment,
    KeySegment,
    MlodySegment,
    PathExpression,
    RecursiveDescentSegment,
    SliceSegment,
    WildcardSegment,
)
from mlody.core.traversal_parser import parse_traversal_expression
from mlody.core.virtual_value import is_virtual_value, lookup_runtime_attribute
from mlody.core.workspace import Workspace


@dataclass(frozen=True)
class SetfAnchor:
    """Resolved workspace-first write target."""

    workspace: Workspace
    resolved_label: str
    target: Anchor
    residual_selector: PathExpression

    @property
    def root_value(self) -> object:
        return self.target.root_value


def _normalize_selector(selector: str | PathExpression) -> PathExpression:
    """Return a parsed selector regardless of the caller input shape."""
    if isinstance(selector, PathExpression):
        return selector
    return parse_traversal_expression(selector)


def _step(current: object, segment: object) -> object:
    """Traverse one direct segment for the current v1 resolver subset."""
    return step_segment(current, segment)


def _children(current: object) -> list[tuple[object, object]]:
    """Return the immediate traversable children of *current*."""
    return list(iter_children(current))


def _has_lineage(value: object) -> bool:
    """Return True when *value* can store `_lineage`."""
    return has_named_child(value, "_lineage")


def _declared_child_contract(
    owner: object,
    segment: object,
    current_value: object,
) -> tuple[object | None, object | None]:
    current_representation = (
        None
        if current_value is MISSING_PLACE_VALUE
        else getattr(current_value, "representation", None)
    )
    if isinstance(segment, FieldSegment):
        attr_spec = lookup_runtime_attribute(owner, segment.name)
        if attr_spec is not None:
            return (
                getattr(attr_spec, "type", None),
                current_representation,
            )
    return (
        getattr(current_value, "type", None),
        current_representation,
    )


def _resolve_contract(values: list[object]) -> tuple[object | None, object | None]:
    """Return the shared type/representation contract for projected values."""
    if not values:
        return (None, None)

    declared_type = getattr(values[0], "type", None)
    representation = getattr(values[0], "representation", None)
    for value in values[1:]:
        if getattr(value, "type", None) != declared_type:
            raise ValueError(
                "projected selection does not share a uniform declared type"
            )
        if getattr(value, "representation", None) != representation:
            raise ValueError(
                "projected selection does not share a uniform representation"
            )
    return (declared_type, representation)


def _validate_new_value(place_set: PlaceSet, new_value: object) -> None:
    """Validate a prospective assignment against the uniform declared type."""
    declared_type = place_set.uniform_type()
    if declared_type is None:
        return

    validator = getattr(declared_type, "validator", None)
    if callable(validator):
        validator(new_value)


def _make_place(
    *,
    root: object,
    owner: object,
    selector: PathExpression,
    current_value: object,
    projected: bool,
    strategy: object,
    declared_type: object | None = None,
    declared_representation: object | None = None,
    lineage_sink: object | None = None,
    lineage_selector: PathExpression | None = None,
) -> Place:
    """Construct a concrete Place instance."""
    return Place(
        root=root,
        owner=owner,
        selector=selector,
        accessor=str(selector),
        current_value=current_value,
        declared_type=declared_type,
        declared_representation=declared_representation,
        strategy=strategy,
        missing=current_value is MISSING_PLACE_VALUE,
        projected=projected,
        lineage_sink=lineage_sink,
        lineage_selector=lineage_selector,
    )


def _make_direct_place(
    *,
    root: object,
    owner: object,
    segment: object,
    prefix: tuple[object, ...],
    current_value: object,
) -> Place:
    """Construct a direct writable place for one concrete segment."""
    selector = PathExpression(segments=prefix)
    lineage_on_current = _has_lineage(current_value)
    lineage_on_owner = _has_lineage(owner)
    runtime_attr = (
        lookup_runtime_attribute(owner, segment.name)
        if isinstance(segment, FieldSegment)
        else None
    )
    if isinstance(segment, FieldSegment) and is_virtual_value(owner):
        strategy = VirtualValueFieldSetter()
    elif (
        isinstance(segment, FieldSegment)
        and runtime_attr is not None
        and getattr(runtime_attr, "_readonly", False)
    ):
        strategy = ReadOnlyFieldSetter()
    elif isinstance(segment, FieldSegment) and (
        is_struct_like(owner) or isinstance(owner, dict)
    ):
        strategy = StructFieldSetter()
    elif (
        isinstance(segment, (FieldSegment, KeySegment))
        and isinstance(owner, dict)
    ):
        strategy = DictKeySetter()
    elif isinstance(segment, IndexSegment) and isinstance(owner, list):
        strategy = ListIndexSetter()
    elif isinstance(segment, KeySegment) and isinstance(owner, dict):
        strategy = DictKeySetter()
    else:
        raise NotImplementedError(
            f"no setter strategy for {type(segment).__name__} on {type(owner).__name__}"
        )
    declared_type, declared_representation = _declared_child_contract(
        owner,
        segment,
        current_value,
    )

    return _make_place(
        root=root,
        owner=owner,
        selector=selector,
        current_value=current_value,
        projected=False,
        strategy=strategy,
        declared_type=declared_type,
        declared_representation=declared_representation,
        lineage_sink=(
            current_value
            if lineage_on_current
            else owner if lineage_on_owner else None
        ),
        lineage_selector=(
            selector
            if lineage_on_current
            else PathExpression(segments=prefix[:-1]) if lineage_on_owner else None
        ),
    )


def _descendants(
    current: object, prefix: tuple[object, ...]
) -> list[tuple[tuple[object, ...], object, object]]:
    """Return all descendants with their concrete selectors and owners."""
    found: list[tuple[tuple[object, ...], object, object]] = []
    for child_segment, child in _children(current):
        child_prefix = prefix + (child_segment,)
        found.append((child_prefix, current, child))
        found.extend(_descendants(child, child_prefix))
    return found


def _resolve_places_recursive(
    *,
    root: object,
    current: object,
    owner: object | None,
    prefix: tuple[object, ...],
    remaining: tuple[object, ...],
) -> list[Place]:
    """Resolve selectors into concrete direct or projected places."""
    if not remaining:
        if owner is None:
            raise ValueError(
                "selector resolved to the root object, not a writable place"
            )
        segment = prefix[-1]
        return [
            _make_direct_place(
                root=root,
                owner=owner,
                segment=segment,
                prefix=prefix,
                current_value=current,
            )
        ]

    segment = remaining[0]
    tail = remaining[1:]

    if isinstance(segment, (FieldSegment, IndexSegment, KeySegment)):
        try:
            child = _step(current, segment)
        except AttributeError:
            if isinstance(segment, FieldSegment) and not tail:
                attr_spec = lookup_runtime_attribute(current, segment.name)
                if attr_spec is not None:
                    return [
                        _make_direct_place(
                            root=root,
                            owner=current,
                            segment=segment,
                            prefix=prefix + (segment,),
                            current_value=MISSING_PLACE_VALUE,
                        )
                    ]
            raise
        return _resolve_places_recursive(
            root=root,
            current=child,
            owner=current,
            prefix=prefix + (segment,),
            remaining=tail,
        )

    if isinstance(segment, SliceSegment):
        if tail:
            raise NotImplementedError(
                "slice traversal followed by more segments is not supported yet"
            )
        current_value = _step(current, segment)
        declared_type, declared_representation = _resolve_contract(current_value)
        return [
            _make_place(
                root=root,
                owner=current,
                selector=PathExpression(segments=prefix + (segment,)),
                current_value=current_value,
                projected=True,
                strategy=SequenceSliceSetter(),
                declared_type=declared_type,
                declared_representation=declared_representation,
                lineage_sink=owner if _has_lineage(owner) else None,
                lineage_selector=PathExpression(segments=prefix[:-1])
                if _has_lineage(owner)
                else None,
            )
        ]

    if isinstance(segment, WildcardSegment):
        places: list[Place] = []
        for child_segment, child in _children(current):
            places.extend(
                _resolve_places_recursive(
                    root=root,
                    current=child,
                    owner=current,
                    prefix=prefix + (child_segment,),
                    remaining=tail,
                )
            )
        return places

    if isinstance(segment, RecursiveDescentSegment):
        places: list[Place] = []
        for descendant_prefix, descendant_owner, descendant in _descendants(
            current, prefix
        ):
            try:
                places.extend(
                    _resolve_places_recursive(
                        root=root,
                        current=descendant,
                        owner=descendant_owner,
                        prefix=descendant_prefix,
                        remaining=tail,
                    )
                )
            except (
                AttributeError,
                IndexError,
                KeyError,
                NotImplementedError,
                TypeError,
            ):
                continue
        return places

    raise NotImplementedError(
        f"selector segment {type(segment).__name__} is not supported yet"
    )


def _replace_one(container: object, segment: object, new_child: object) -> object:
    """Return *container* with *segment* replaced by *new_child*."""
    return replace_child(container, segment, new_child)


def _rebuild_owner(
    root: object, prefix: tuple[object, ...], new_owner: object
) -> object:
    """Rebuild the path from *root* to *new_owner* over the given prefix."""
    if not prefix:
        return new_owner

    segment = prefix[0]
    child = _step(root, segment)
    rebuilt_child = _rebuild_owner(child, prefix[1:], new_owner)
    return _replace_one(root, segment, rebuilt_child)


def _replace_path_value(
    root: object, path: tuple[object, ...], new_value: object
) -> object:
    """Return *root* with the exact *path* replaced by *new_value*."""
    if not path:
        return new_value
    segment = path[0]
    child = _step(root, segment)
    rebuilt_child = _replace_path_value(child, path[1:], new_value)
    return _replace_one(root, segment, rebuilt_child)


def resolve_places(root: object, selector: str | PathExpression) -> PlaceSet:
    """Resolve a selector into writable places."""
    parsed = _normalize_selector(selector)
    if not parsed.segments:
        raise ValueError("selector resolved to no writable places")
    places = _resolve_places_recursive(
        root=root,
        current=root,
        owner=None,
        prefix=(),
        remaining=parsed.segments,
    )
    return PlaceSet(places=tuple(places))


def can_setf(
    root: object,
    selector: str | PathExpression,
    new_value: object,
    *,
    mode: AssignmentMode = "inplace",
) -> None:
    """Validate a prospective selector-based assignment without committing it."""
    _ = new_value
    place_set = resolve_places(root, selector)
    place_set.assert_non_empty()
    place_set.assert_uniform_contract()
    _validate_new_value(place_set, new_value)
    for place in place_set.places:
        place.strategy.preflight(place, new_value, mode=mode)


def setf_root(
    root: object,
    selector: str | PathExpression,
    new_value: object,
    *,
    mode: AssignmentMode = "inplace",
    source: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> object:
    """Apply a selector-based assignment and return the updated root."""
    place_set = resolve_places(root, selector)
    place_set.assert_non_empty()
    can_setf(root, selector, new_value, mode=mode)
    working_root = root
    for place in place_set.places:
        fresh_place_set = resolve_places(working_root, place.selector)
        if len(fresh_place_set.places) != 1:
            raise ValueError(
                "concrete place selector did not resolve to exactly one place"
            )
        fresh_place = fresh_place_set.places[0]
        new_owner = fresh_place.strategy.commit(fresh_place, new_value, mode=mode)
        working_root = _rebuild_owner(
            working_root,
            fresh_place.selector.segments[:-1],
            new_owner,
        )
        if fresh_place.lineage_selector is not None:
            event = build_lineage_event(
                accessor=fresh_place.accessor,
                new_value=new_value,
                source=source,
                reason=reason,
                timestamp=timestamp,
                mode=mode,
            )
            if not fresh_place.lineage_selector.segments:
                working_root = append_lineage(working_root, event, mode=mode)
            else:
                sink = (
                    resolve_places(working_root, fresh_place.lineage_selector)
                    .places[0]
                    .current_value
                )
                updated_sink = append_lineage(sink, event, mode=mode)
                working_root = _replace_path_value(
                    working_root,
                    fresh_place.lineage_selector.segments,
                    updated_sink,
                )
    return working_root


def _selector_from_label_anchor(anchor: Anchor) -> PathExpression:
    """Convert workspace anchor residuals into a traversal selector."""
    segments: list[object] = [FieldSegment(field) for field in anchor.field_parts]
    if anchor.entity_query is not None:
        query_expression = parse_traversal_expression(f"[{anchor.entity_query}]")
        if not (
            query_expression.segments
            and isinstance(query_expression.segments[0], MlodySegment)
        ):
            segments.extend(query_expression.segments)
    return PathExpression(segments=tuple(segments))


def _load_cwd_workspace() -> Workspace:
    """Load the current working-directory workspace for unqualified labels."""
    workspace = Workspace(monorepo_root=Path.cwd())
    workspace.load()
    return workspace


def resolve_setf_anchor(
    ref: str,
    *,
    workspace: Workspace | None = None,
) -> SetfAnchor:
    """Resolve a single concrete label reference into a writable setf anchor."""
    label = parse_ref_label(ref)
    if label.entity is None and label.attribute_path is None:
        msg = f"setf requires a label reference, got {ref!r}"
        raise ValueError(msg)
    if label.workspace is not None:
        msg = (
            "setf labels must be relative to the current workspace; "
            f"got explicit workspace qualifier in {ref!r}"
        )
        raise ValueError(msg)

    authoritative_workspace = workspace or _load_cwd_workspace()
    inner_label = label.format_inner()

    label_anchor = authoritative_workspace.resolve_label_anchor(inner_label)
    return SetfAnchor(
        workspace=authoritative_workspace,
        resolved_label=inner_label,
        target=label_anchor,
        residual_selector=_selector_from_label_anchor(label_anchor),
    )


def _resolve_setf_anchors(
    ref: str,
    *,
    workspace: Workspace | None = None,
) -> list[SetfAnchor]:
    """Resolve a possibly-wildcard label into concrete writable anchors."""
    label = parse_ref_label(ref)
    if label.entity is None and label.attribute_path is None:
        msg = f"setf requires a label reference, got {ref!r}"
        raise ValueError(msg)
    if label.workspace is not None:
        msg = (
            "setf labels must be relative to the current workspace; "
            f"got explicit workspace qualifier in {ref!r}"
        )
        raise ValueError(msg)

    authoritative_workspace = workspace or _load_cwd_workspace()
    inner_label = label.format_inner()

    concrete_labels = authoritative_workspace.expand_wildcard_label(inner_label)
    anchors: list[SetfAnchor] = []
    for concrete_label in concrete_labels:
        label_anchor = authoritative_workspace.resolve_label_anchor(concrete_label)
        anchors.append(
            SetfAnchor(
                workspace=authoritative_workspace,
                resolved_label=concrete_label,
                target=label_anchor,
                residual_selector=_selector_from_label_anchor(label_anchor),
            )
        )
    return anchors


def _validate_root_assignment(root_value: object, new_value: object) -> None:
    """Validate whole-root replacement against the declared type when present."""
    declared_type = getattr(root_value, "type", None)
    validator = getattr(declared_type, "validator", None)
    if callable(validator):
        validator(new_value)


def _preflight_anchor(
    anchor: SetfAnchor,
    new_value: object,
    *,
    mode: AssignmentMode,
) -> None:
    """Validate that an anchor can accept the proposed assignment."""
    anchor.target.ensure_writable()
    if not anchor.residual_selector.segments:
        _validate_root_assignment(anchor.root_value, new_value)
        return
    can_setf(anchor.root_value, anchor.residual_selector, new_value, mode=mode)


def _apply_anchor_assignment(
    anchor: SetfAnchor,
    new_value: object,
    *,
    mode: AssignmentMode,
    source: str | None,
    reason: str | None,
    timestamp: str | None,
) -> object:
    """Apply an assignment against a resolved anchor root."""
    if not anchor.residual_selector.segments:
        updated_root = new_value
        if _has_lineage(updated_root):
            event = build_lineage_event(
                accessor=anchor.resolved_label,
                new_value=new_value,
                source=source,
                reason=reason,
                timestamp=timestamp,
                mode=mode,
            )
            updated_root = append_lineage(updated_root, event, mode=mode)
        return updated_root
    return setf_root(
        anchor.root_value,
        anchor.residual_selector,
        new_value,
        mode=mode,
        source=source,
        reason=reason,
        timestamp=timestamp,
    )


def _write_back_anchor(anchor: SetfAnchor, updated_root: object) -> None:
    """Persist an updated anchor root back into the owning workspace."""
    anchor.target.write_back(anchor.workspace.registry_view, updated_root)


def setf(
    ref: str,
    new_value: object,
    *,
    workspace: Workspace | None = None,
    mode: AssignmentMode = "inplace",
    source: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> Workspace:
    """Apply a label-aware assignment and return the updated workspace."""
    anchors = _resolve_setf_anchors(ref, workspace=workspace)
    if not anchors:
        msg = f"selector {ref!r} resolved to no writable places"
        raise ValueError(msg)

    for anchor in anchors:
        _preflight_anchor(anchor, new_value, mode=mode)

    for anchor in anchors:
        updated_root = _apply_anchor_assignment(
            anchor,
            new_value,
            mode=mode,
            source=source,
            reason=reason,
            timestamp=timestamp,
        )
        _write_back_anchor(anchor, updated_root)

    return anchors[0].workspace
