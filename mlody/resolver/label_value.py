"""Label → MlodyValue resolution step.

This module adds the third step of the mlody show pipeline:

    parse_label(target_str)          → Label           [existing]
    workspace.expand_wildcard_label  → [Label, ...]    [existing]
    resolve_label_to_value(          → MlodyValue      [this module]
        label, workspace)

Public entry point: ``resolve_label_to_value``.
Re-exported from ``mlody.resolver``.

Extension seam (design D-3):
    The dispatch table ``TRAVERSAL_STRATEGIES`` maps kind strings to
    ``TraversalStrategy`` instances.  Adding a callable-based strategy for a
    future kind (e.g. one that lazily derives a value from the Workspace rather
    than from a static Struct field) requires only:
      1. Implement a class conforming to ``TraversalStrategy``.
      2. Add one entry to ``TRAVERSAL_STRATEGIES``.
    No changes to ``resolve_label_to_value`` or ``show`` are needed.

See also: design.md §D-3, §D-6.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence, Union

if TYPE_CHECKING:
    from mlody.core.label.label import Label
    from mlody.core.workspace import Workspace


# ---------------------------------------------------------------------------
# Value type hierarchy  (tasks 1.1 – 1.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MlodyValue:
    """Base class for all resolved mlody values."""


@dataclass(frozen=True)
class MlodyWorkspaceValue(MlodyValue):
    """The workspace itself (label has no entity spec).

    ``name`` is the workspace name (``label.workspace``), or ``None`` for CWD.
    ``root`` is the absolute filesystem path of the monorepo root.
    """

    name: str | None
    root: str


@dataclass(frozen=True)
class MlodyFolderValue(MlodyValue):
    """A directory on disk under the workspace.

    ``path`` is workspace-relative (without leading slash), matching the label.
    ``children`` contains the names of the immediate directory entries.
    """

    path: str
    children: list[str]  # pyright: ignore[reportMutableClassVariable]


@dataclass(frozen=True)
class MlodySourceValue(MlodyValue):
    """A ``.mlody`` source file on disk.

    ``path`` is the workspace-relative path **without** the ``.mlody`` suffix,
    matching the label exactly.
    """

    path: str


@dataclass(frozen=True)
class MlodyTaskValue(MlodyValue):
    """Opaque wrapper around a task registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyActionValue(MlodyValue):
    """Opaque wrapper around an action registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyValueValue(MlodyValue):
    """Opaque wrapper around a value registry Struct."""

    struct: object


@dataclass(frozen=True)
class MlodyUnresolvedValue(MlodyValue):
    """Soft-failure sentinel.

    Returned (never raised) when any resolution step cannot proceed.
    ``reason`` is a human-readable string naming the failed step.
    """

    label: "Label"
    reason: str


@dataclass(frozen=True)
class MlodyVectorValue(MlodyValue):
    """A collection of ``MlodyValue`` elements produced by wildcard or recursive-descent traversal.

    ``elements`` is a tuple of ``MlodyValue`` instances in deterministic order
    (declaration order for wildcards, depth-first for recursive descent).
    """

    elements: tuple[MlodyValue, ...]


class TraversalErrorPolicy(enum.Enum):
    """Per-call-site error policy for traversal engine steps.

    ``SKIP``: when a step cannot proceed (missing field, out-of-bounds index,
    type mismatch), produce no output for that branch and continue silently.

    ``RAISE``: return ``MlodyUnresolvedValue`` immediately on the first
    unresolvable step (consistent with existing behaviour; this is the default).
    """

    SKIP = "skip"
    RAISE = "raise"


# ---------------------------------------------------------------------------
# Traversal strategy protocol  (task 2.1)
# ---------------------------------------------------------------------------


class TraversalStrategy(Protocol):
    """Contract for attribute-path traversal per entity kind.

    v1 ships ``StructTraversalStrategy`` for task and action.
    Future callable-based strategies (e.g. lazy workspace-info computation)
    implement this protocol without touching ``resolve_label_to_value``.

    The optional ``traversal_error_policy`` keyword argument (design D-4)
    defaults to ``RAISE`` for backward compatibility with existing implementations
    that do not declare it.
    """

    def traverse(
        self,
        value: object,
        path: tuple[str, ...] | tuple[object, ...],
        label: "Label",
        *,
        traversal_error_policy: TraversalErrorPolicy = TraversalErrorPolicy.RAISE,
    ) -> MlodyValue: ...


# ---------------------------------------------------------------------------
# Struct-based traversal strategy  (task 2.2)
# ---------------------------------------------------------------------------


def _wrap_struct(kind: str, struct: object) -> MlodyValue:
    """Wrap a registry struct in its typed MlodyValue subclass."""
    if kind == "task":
        return MlodyTaskValue(struct=struct)
    if kind == "action":
        return MlodyActionValue(struct=struct)
    if kind == "value":
        return MlodyValueValue(struct=struct)
    # Future kinds added to the dispatch table will provide their own wrapper;
    # this function is called after kind dispatch, so this branch is unreachable
    # for registered kinds.
    return MlodyUnresolvedValue(
        label=_SENTINEL_LABEL,  # replaced by callers that know the label
        reason=f"no wrapper defined for kind {kind!r}",
    )


class StructTraversalStrategy:
    """Attribute-path traversal via getattr on a Starlark Struct.

    Walks each segment in ``path`` via ``getattr``.  Returns
    ``MlodyUnresolvedValue`` immediately on the first ``AttributeError``
    rather than propagating the exception to callers (design R-001).

    The terminal value is returned as-is (the raw Python object); callers are
    responsible for wrapping it if a typed ``MlodyValue`` is desired.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def traverse(
        self,
        value: object,
        path: tuple[str, ...],
        label: "Label",
        **kwargs: object,  # Accepts traversal_error_policy for protocol compatibility (D-4)
    ) -> MlodyValue:
        if not path:
            return _wrap_struct(self._kind, value)

        obj: object = value
        for i, segment in enumerate(path):
            try:
                obj = getattr(obj, segment)
            except AttributeError:
                traversed = ".".join(path[:i])
                parent = f" on '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute '{segment}' not found"
                        f"{parent} (label: {label!r})"
                    ),
                )
        # Terminal value reached — if it's a known entity kind (e.g. the .action
        # field on a task struct is itself an action), wrap it properly so the
        # caller gets a typed MlodyValue rather than a raw dump.
        terminal_kind = getattr(obj, "kind", None)
        if isinstance(terminal_kind, str) and terminal_kind in TRAVERSAL_STRATEGIES:
            return _wrap_struct(terminal_kind, obj)
        return _RawAttrValue(value=obj, label=label)


@dataclass(frozen=True)
class _RawAttrValue(MlodyValue):
    """Internal: terminal value reached after attribute-path traversal."""

    value: object
    label: "Label"


def _is_top_type(type_obj: object) -> bool:
    if getattr(type_obj, "kind", None) != "type":
        return False
    return (
        getattr(type_obj, "name", None) == "top"
        or getattr(type_obj, "type", None) == "top"
        or getattr(type_obj, "_root_kind", None) == "top"
    )


def _is_json_representation(rep_obj: object) -> bool:
    return (
        getattr(rep_obj, "kind", None) == "representation"
        and getattr(rep_obj, "name", None) == "json"
    )


def _posix_location_paths(location: object) -> list[str]:
    if getattr(location, "type", None) != "posix":
        return []
    path_value = getattr(location, "path", None)
    if path_value is None and isinstance(getattr(location, "attributes", None), dict):
        path_value = location.attributes.get("path")
    if path_value is None:
        return []
    if isinstance(path_value, str):
        return [path_value]
    if isinstance(path_value, (list, tuple)):
        return [str(p) for p in path_value]
    return [str(path_value)]


def _is_parquet_backed(value: object) -> bool:
    """Return True when *value* has a parquet representation."""
    rep = getattr(value, "representation", None)
    return (
        getattr(rep, "name", None) == "parquet"
        or getattr(rep, "type", None) == "parquet"
    )


def _traverse_json_backed_value(
    value: object,
    path: tuple[str, ...],
    label: "Label",
) -> MlodyValue | None:
    """Traverse JSON content for top/json/posix values.

    Returns ``None`` when the value is not eligible for JSON-backed traversal.
    Returns ``MlodyUnresolvedValue`` for eligible-but-failed traversal.
    Returns ``_RawAttrValue`` on success.
    """
    value_type = getattr(value, "type", None)
    representation = getattr(value, "representation", None)
    if not (_is_top_type(value_type) and _is_json_representation(representation)):
        return None

    location = getattr(value, "location", None)
    paths = _posix_location_paths(location)
    if not paths:
        return MlodyUnresolvedValue(
            label=label,
            reason=(
                "json-backed traversal currently requires a posix location path; "
                f"got location type {getattr(location, 'type', None)!r}"
            ),
        )

    existing = [os.path.expanduser(p) for p in paths if os.path.isfile(os.path.expanduser(p))]
    if not existing:
        return MlodyUnresolvedValue(
            label=label,
            reason=(
                "json-backed traversal could not find a readable file at location paths: "
                f"{paths!r}"
            ),
        )
    if len(existing) > 1:
        return MlodyUnresolvedValue(
            label=label,
            reason=(
                "json-backed traversal requires a single file, but multiple files were found: "
                f"{existing!r}"
            ),
        )

    json_path = existing[0]
    try:
        with open(json_path, encoding="utf-8") as fh:
            current: object = json.load(fh)
    except Exception as exc:
        return MlodyUnresolvedValue(
            label=label,
            reason=f"failed to parse JSON at {json_path!r}: {exc}",
        )

    for i, segment in enumerate(path):
        if isinstance(current, dict):
            if segment not in current:
                traversed = ".".join(path[:i])
                parent = f" under '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"json key {segment!r} not found{parent} in {json_path!r} "
                        f"(label: {label!r})"
                    ),
                )
            current = current[segment]
            continue

        if isinstance(current, list):
            try:
                idx = int(segment)
            except ValueError:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"expected numeric list index while traversing JSON, got {segment!r} "
                        f"(label: {label!r})"
                    ),
                )
            if idx < 0 or idx >= len(current):
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"list index {idx} out of range while traversing JSON "
                        f"(len={len(current)}, label: {label!r})"
                    ),
                )
            current = current[idx]
            continue

        return MlodyUnresolvedValue(
            label=label,
            reason=(
                f"cannot traverse segment {segment!r} on JSON value of type "
                f"{type(current).__name__} (label: {label!r})"
            ),
        )

    return _RawAttrValue(value=current, label=label)


# ---------------------------------------------------------------------------
# Traversal engine helpers  (tasks 3.3 – 3.7)
# ---------------------------------------------------------------------------
#
# Each helper accepts the _current_ MlodyValue, the typed PathSegment, the
# error policy, and the originating label.  They never raise; all failures
# produce MlodyUnresolvedValue (RAISE) or MlodyVectorValue(elements=()) (SKIP).


def _policy_miss(
    policy: TraversalErrorPolicy,
    label: "Label",
    reason: str,
) -> MlodyValue:
    """Shared helper: convert a "miss" into the policy-appropriate MlodyValue."""
    if policy is TraversalErrorPolicy.SKIP:
        return MlodyVectorValue(elements=())
    return MlodyUnresolvedValue(label=label, reason=reason)


def _wrap_raw(obj: object, label: "Label") -> MlodyValue:
    """Wrap a raw Python value produced by engine traversal."""
    if isinstance(obj, MlodyValue):
        return obj
    return _RawAttrValue(value=obj, label=label)


def _is_record_struct(value: object) -> bool:
    """Return True when *value* is a Starlark Struct with a record type."""
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    if not isinstance(value, _Struct):
        return False
    value_type = getattr(value, "type", None)
    return (
        getattr(value_type, "kind", None) == "record"
        or getattr(value_type, "_root_kind", None) == "record"
    )


def _engine_index_step(
    current: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Apply an IndexSegment to *current*.

    Supported inputs:
    - ``MlodyVectorValue``: index into ``elements`` tuple.

    Out-of-bounds and type mismatches follow the RAISE/SKIP policy.
    """
    from mlody.core.traversal_grammar import IndexSegment  # noqa: PLC0415

    assert isinstance(segment, IndexSegment)
    idx = segment.index

    if isinstance(current, MlodyVectorValue):
        elems = current.elements
        try:
            return elems[idx]
        except IndexError:
            return _policy_miss(
                policy,
                label,
                (
                    f"index {idx} is out of range for vector of length {len(elems)} "
                    f"(label: {label!r})"
                ),
            )

    if isinstance(current, _RawAttrValue) and isinstance(current.value, (list, tuple)):
        seq = current.value
        try:
            return _RawAttrValue(value=seq[idx], label=label)
        except IndexError:
            return _policy_miss(
                policy,
                label,
                (
                    f"index {idx} is out of range for sequence of length {len(seq)} "
                    f"(label: {label!r})"
                ),
            )

    if isinstance(current, MlodyValueValue) and isinstance(current.struct, (list, tuple)):
        seq = current.struct
        try:
            return MlodyValueValue(struct=seq[idx])
        except IndexError:
            return _policy_miss(
                policy,
                label,
                (
                    f"index {idx} is out of range for sequence of length {len(seq)} "
                    f"(label: {label!r})"
                ),
            )

    return _policy_miss(
        policy,
        label,
        (
            f"IndexSegment requires a vector value but got "
            f"{type(current).__name__} (label: {label!r})"
        ),
    )


def _engine_key_step(
    current: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Apply a KeySegment to *current*.

    Supported inputs:
    - ``_RawAttrValue`` whose ``value`` is a Python ``dict``.

    Missing keys and type mismatches follow the RAISE/SKIP policy.
    """
    from mlody.core.traversal_grammar import KeySegment  # noqa: PLC0415

    assert isinstance(segment, KeySegment)
    key = segment.key

    d: object = None
    if isinstance(current, _RawAttrValue) and isinstance(current.value, dict):
        d = current.value
    elif isinstance(current, MlodyValue):
        # Check if the wrapped struct has a dict-like value somewhere
        pass

    if isinstance(d, dict):
        if key in d:
            return _wrap_raw(d[key], label)
        return _policy_miss(
            policy,
            label,
            f"key {key!r} not found in dict (label: {label!r})",
        )

    return _policy_miss(
        policy,
        label,
        (
            f"KeySegment requires a dict-backed value but got "
            f"{type(current).__name__} (label: {label!r})"
        ),
    )


def _engine_slice_step(
    current: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Apply a SliceSegment to *current*.

    Supported inputs:
    - ``MlodyVectorValue``: slice the ``elements`` tuple → new ``MlodyVectorValue``.
    - ``_RawAttrValue`` whose ``value`` is a Python list or tuple → ``MlodyVectorValue``.
    - ``MlodyValueValue`` whose ``struct`` is a Python list or tuple → ``MlodyVectorValue``.

    Type mismatches follow the RAISE/SKIP policy.
    """
    from mlody.core.traversal_grammar import SliceSegment  # noqa: PLC0415

    assert isinstance(segment, SliceSegment)
    sl = slice(segment.start, segment.stop, segment.step)

    if isinstance(current, MlodyVectorValue):
        sliced = current.elements[sl]
        return MlodyVectorValue(elements=tuple(sliced))

    if isinstance(current, _RawAttrValue) and isinstance(current.value, (list, tuple)):
        sliced_raw = current.value[sl]
        return MlodyVectorValue(elements=tuple(_wrap_raw(v, label) for v in sliced_raw))

    if isinstance(current, MlodyValueValue) and isinstance(current.struct, (list, tuple)):
        sliced_struct = current.struct[sl]
        return MlodyVectorValue(elements=tuple(MlodyValueValue(struct=v) for v in sliced_struct))

    return _policy_miss(
        policy,
        label,
        (
            f"SliceSegment requires a vector or sequence value but got "
            f"{type(current).__name__} (label: {label!r})"
        ),
    )


def _collect_record_fields(
    value: object,
    label: "Label",
) -> list[MlodyValue]:
    """Collect all immediate children of a record-typed Struct.

    Uses ``_traverse_one_step`` so that each child gets a composed location.
    Returns an empty list for non-record or empty structs.
    """
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    if not isinstance(value, _Struct):
        return []
    value_type = getattr(value, "type", None)
    is_record = (
        getattr(value_type, "kind", None) == "record"
        or getattr(value_type, "_root_kind", None) == "record"
    )
    if not is_record:
        return []

    _direct_fields = getattr(value_type, "fields", None)
    _attrs_dict = getattr(value_type, "attributes", None)
    _attrs_fields = _attrs_dict.get("fields") if isinstance(_attrs_dict, dict) else None
    fields_list: list[object] = list(_direct_fields or _attrs_fields or [])

    children: list[MlodyValue] = []
    for f in fields_list:
        fname = getattr(f, "name", None)
        if not isinstance(fname, str):
            continue
        result = _traverse_one_step(value, fname, (), label, TraversalErrorPolicy.RAISE)
        if isinstance(result, MlodyUnresolvedValue):
            continue
        if isinstance(result, tuple):
            rebuilt, _ = result
            children.append(MlodyValueValue(struct=rebuilt))
        elif isinstance(result, MlodyValue):
            children.append(result)
    return children


def _engine_wildcard_step(
    current: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Apply a WildcardSegment to *current*.

    Supported inputs (priority order):
    1. ``MlodyVectorValue`` → return all elements.
    2. ``_RawAttrValue`` whose ``value`` is a Python ``dict`` → return dict values.
    3. Record-typed Starlark Struct → return all declared fields via
       ``_traverse_one_step``.

    Non-traversable roots follow the RAISE/SKIP policy.
    """
    # Case 1: vector
    if isinstance(current, MlodyVectorValue):
        return MlodyVectorValue(elements=current.elements)

    # Case 2: dict-backed
    if isinstance(current, _RawAttrValue) and isinstance(current.value, dict):
        children: list[MlodyValue] = [_wrap_raw(v, label) for v in current.value.values()]
        return MlodyVectorValue(elements=tuple(children))

    # Case 3: record-typed Struct
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    if isinstance(current, (MlodyValueValue, MlodyTaskValue, MlodyActionValue)):
        struct_obj = current.struct  # type: ignore[union-attr]
        if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
            field_values = _collect_record_fields(struct_obj, label)
            return MlodyVectorValue(elements=tuple(field_values))

    # Also try the raw struct when current is MlodyValueValue and struct is a Struct
    if isinstance(current, MlodyValueValue):
        struct_obj = current.struct
        if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
            field_values = _collect_record_fields(struct_obj, label)
            return MlodyVectorValue(elements=tuple(field_values))

    # Fallback: check if current itself is a Struct that is record-typed
    # (handles case where the Struct is passed directly, not wrapped)
    if isinstance(current, _Struct) and _is_record_struct(current):  # type: ignore[arg-type]
        field_values = _collect_record_fields(current, label)
        return MlodyVectorValue(elements=tuple(field_values))

    return _policy_miss(
        policy,
        label,
        (
            f"WildcardSegment cannot traverse {type(current).__name__}; "
            "expected a vector, dict-backed value, or record-typed Struct "
            f"(label: {label!r})"
        ),
    )


def _engine_recursive_descent_step(
    current: MlodyValue,
    segment: object,
    policy: TraversalErrorPolicy,
    label: "Label",
) -> MlodyValue:
    """Apply a RecursiveDescentSegment to *current*.

    Collects all descendants at any depth using depth-first traversal.
    The current value itself is NOT included.  Recurses into:
    - ``MlodyVectorValue`` elements
    - ``_RawAttrValue`` wrapping a Python ``dict`` (values)
    - Record-typed Starlark Structs (all declared fields)

    Does not recurse into scalar leaves (non-Struct, non-dict, non-list).
    Non-traversable roots follow the RAISE/SKIP policy.
    """
    collected: list[MlodyValue] = []
    _visited: set[int] = set()

    def _collect_children(node: object) -> list[MlodyValue]:
        """Return the immediate MlodyValue children of *node*.

        Accepts both typed ``MlodyValue`` wrappers and raw Starlark Structs so
        the engine can be called directly with an unwrapped struct (e.g. from
        tests or mapped-traversal intermediates) without extra wrapping.
        """
        from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

        if isinstance(node, MlodyVectorValue):
            return list(node.elements)

        if isinstance(node, _RawAttrValue) and isinstance(node.value, dict):
            return [_wrap_raw(v, label) for v in node.value.values()]

        if isinstance(node, (MlodyValueValue, MlodyTaskValue, MlodyActionValue)):
            struct_obj = node.struct  # type: ignore[union-attr]
            if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
                return _collect_record_fields(struct_obj, label)

        # Raw Struct with record type — reached for the initial root or for fields
        # produced by _collect_record_fields whose traversal rebuilt a raw Struct.
        if isinstance(node, _Struct) and _is_record_struct(node):  # type: ignore[arg-type]
            return _collect_record_fields(node, label)  # type: ignore[arg-type]

        return []

    def _dfs(node: object) -> None:
        node_id = id(node)
        if node_id in _visited:
            return
        _visited.add(node_id)
        children = _collect_children(node)
        for child in children:
            collected.append(child)
            _dfs(child)

    # Check that the root is traversable (not a scalar/unresolvable leaf)
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    root_is_traversable = (
        isinstance(current, MlodyVectorValue)
        or (isinstance(current, _RawAttrValue) and isinstance(current.value, dict))
        or (
            isinstance(current, (MlodyValueValue, MlodyTaskValue, MlodyActionValue))
            and isinstance(getattr(current, "struct", None), _Struct)
            and _is_record_struct(getattr(current, "struct", None))
        )
        # Raw Struct with record type — reached when the engine is called directly
        # with an unwrapped struct (e.g. from tests or mapped-traversal intermediate).
        or (isinstance(current, _Struct) and _is_record_struct(current))  # type: ignore[arg-type]
    )

    if not root_is_traversable:
        return _policy_miss(
            policy,
            label,
            (
                f"RecursiveDescentSegment cannot traverse {type(current).__name__}; "
                "expected a vector, dict-backed value, or record-typed Struct "
                f"(label: {label!r})"
            ),
        )

    _dfs(current)
    return MlodyVectorValue(elements=tuple(collected))


# A sentinel label used only in error messages from _wrap_struct above.
# It is never returned to callers because _wrap_struct is called only when
# kind is in the dispatch table (which provides typed wrappers).
class _SentinelEntitySpec:
    root = None
    path = None
    wildcard = False
    name = None
    field_path = None


class _SentinelLabel:
    workspace = None
    workspace_query = None
    entity = _SentinelEntitySpec()
    entity_query = None
    attribute_path = None
    attribute_query = None

    def __repr__(self) -> str:
        return "<sentinel>"


_SENTINEL_LABEL: "Label" = _SentinelLabel()  # type: ignore[assignment]


def _traverse_one_step(
    current_struct: object,
    field_name: object,  # str | PathSegment
    path_so_far: tuple[object, ...],
    label: "Label",
    policy: TraversalErrorPolicy = TraversalErrorPolicy.RAISE,
) -> tuple[object, bool] | MlodyValue:
    """Perform one step of record-aware field traversal on a Starlark Struct.

    Accepts either a plain ``str`` or a typed ``PathSegment`` (design R-1
    backward-compatibility guarantee).  A bare ``str`` is wrapped in
    ``FieldSegment`` internally.

    When the segment is a ``FieldSegment`` (or a plain ``str``), applies
    record-aware field lookup and ``compose_location()`` and returns a
    ``(rebuilt_struct, False)`` tuple on success.

    For other ``PathSegment`` kinds (``IndexSegment``, ``KeySegment``,
    ``WildcardSegment``, ``RecursiveDescentSegment``), delegates to the
    corresponding engine helper and returns a ``MlodyValue`` directly.

    Args:
        current_struct: The current value at this traversal level.
        field_name: Path segment — ``str`` (legacy) or ``PathSegment`` (typed).
        path_so_far: Segments already consumed, used only in error messages.
        label: The originating Label, used only in error messages.
        policy: Error policy for non-field dispatch (RAISE or SKIP).

    Returns:
        ``(rebuilt_struct, False)`` for FieldSegment/str on a Struct, or a
        ``MlodyValue`` for other segment kinds or any failure.
    """
    from mlody.core.traversal_grammar import (  # noqa: PLC0415
        FieldSegment,
        IndexSegment,
        KeySegment,
        PathSegment,
        RecursiveDescentSegment,
        SliceSegment,
        WildcardSegment,
    )
    from mlody.core.location_composition import (  # noqa: PLC0415
        LocationComposeError,
        compose_location,
    )
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    # Normalise: wrap plain str in FieldSegment for unified dispatch.
    if isinstance(field_name, str):
        segment: PathSegment = FieldSegment(name=field_name)
        effective_name: str = field_name
    elif isinstance(field_name, FieldSegment):
        segment = field_name
        effective_name = field_name.name
    elif isinstance(field_name, IndexSegment):
        # Delegate to engine helper — current_struct must be a MlodyValue
        if isinstance(current_struct, MlodyValue):
            return _engine_index_step(current_struct, field_name, policy, label)
        return _engine_index_step(MlodyValueValue(struct=current_struct), field_name, policy, label)
    elif isinstance(field_name, KeySegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_key_step(current_struct, field_name, policy, label)
        return _engine_key_step(_RawAttrValue(value=current_struct, label=label), field_name, policy, label)
    elif isinstance(field_name, WildcardSegment):
        # Wildcard: expand current struct as a record-typed Struct value
        if isinstance(current_struct, MlodyValue):
            return _engine_wildcard_step(current_struct, field_name, policy, label)
        # Wrap as MlodyValueValue so engine handles it
        return _engine_wildcard_step(MlodyValueValue(struct=current_struct), field_name, policy, label)
    elif isinstance(field_name, RecursiveDescentSegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_recursive_descent_step(current_struct, field_name, policy, label)
        return _engine_recursive_descent_step(MlodyValueValue(struct=current_struct), field_name, policy, label)
    elif isinstance(field_name, SliceSegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_slice_step(current_struct, field_name, policy, label)
        return _engine_slice_step(MlodyValueValue(struct=current_struct), field_name, policy, label)
    else:
        return MlodyUnresolvedValue(
            label=label,
            reason=f"unknown path segment type {type(field_name).__name__!r}",
        )

    # FieldSegment / str path: record-aware field lookup.
    # If current_struct is a typed MlodyValue wrapper (MlodyValueValue, MlodyTaskValue,
    # or MlodyActionValue), unwrap to the inner Struct so that field lookup finds the
    # record type.  This is required for mapped traversal (task 4.3): after a wildcard
    # expands elements into MlodyValueValue instances, subsequent FieldSegment steps
    # must operate on the underlying Starlark Struct, not the Python wrapper.
    if isinstance(current_struct, (MlodyValueValue, MlodyTaskValue, MlodyActionValue)):
        current_struct = current_struct.struct  # type: ignore[union-attr]
    value_type = getattr(current_struct, "type", None)
    _SENTINEL = object()

    # Field lookup order:
    # 1. Search type.fields for a matching entry by name.
    # 2. Fall back to getattr(value.type, effective_name).
    # 3. If both miss, return MlodyUnresolvedValue.
    _direct_fields = getattr(value_type, "fields", None)
    _attrs_dict = getattr(value_type, "attributes", None)
    _attrs_fields = _attrs_dict.get("fields") if isinstance(_attrs_dict, dict) else None
    fields_list: list[object] = list(_direct_fields or _attrs_fields or [])

    field_obj: object = _SENTINEL
    for f in fields_list:
        if getattr(f, "name", None) == effective_name:
            field_obj = f
            break

    if field_obj is _SENTINEL:
        fallback = getattr(value_type, effective_name, _SENTINEL)
        if fallback is _SENTINEL:
            available = [str(getattr(f, "name", "?")) for f in fields_list]
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"field {effective_name!r} not found on record type "
                    f"{getattr(value_type, 'name', '?')!r}; "
                    f"available fields: {available}"
                ),
            )
        # Direct type attribute fallback (non-Struct): return as _RawAttrValue.
        return _RawAttrValue(value=fallback, label=label)

    parent_loc = getattr(current_struct, "location", None)
    field_loc = getattr(field_obj, "location", None)
    try:
        composed_loc = compose_location(
            parent_loc=parent_loc,  # type: ignore[arg-type]
            field_loc=field_loc,  # type: ignore[arg-type]
            field_name=effective_name,
        )
    except LocationComposeError as exc:
        return MlodyUnresolvedValue(label=label, reason=str(exc))

    # Rebuild the field struct with the composed location substituted.
    # Use as_mapping() (not _fields) to capture every declared field,
    # including those added via extend_attrs (R-009).
    if isinstance(field_obj, _Struct):
        field_map = dict(field_obj.as_mapping())
        field_map["location"] = composed_loc
        rebuilt = _Struct(**field_map)
    else:
        # Non-Struct field_obj: return as _RawAttrValue consistent with the
        # existing single-level branch behaviour.
        return _RawAttrValue(value=field_obj, label=label)

    return (rebuilt, False)


class ValueTraversalStrategy:
    """Record-aware traversal strategy for ``kind="value"`` structs.

    When ``path`` is non-empty and the value has a record type
    (``type.kind == "record"`` or ``type._root_kind == "record"``), applies
    record-aware field lookup and ``compose_location()`` at every step of the
    path, accumulating the composed location through all levels.  Uses the
    shared ``_traverse_one_step`` helper for each step.

    For an empty path, wraps the struct as ``MlodyValueValue``.
    For non-record root values, falls back to generic ``getattr`` traversal
    (the OQ-13 extension seam).

    The optional ``traversal_error_policy`` keyword argument (design D-4)
    controls SKIP/RAISE behaviour for non-field segment types.
    """

    def traverse(
        self,
        value: object,
        path: tuple[object, ...],
        label: "Label",
        *,
        traversal_error_policy: TraversalErrorPolicy = TraversalErrorPolicy.RAISE,
    ) -> MlodyValue:
        from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415
        from mlody.core.virtual_value import traverse_virtual_value  # noqa: PLC0415
        from mlody.core.traversal_grammar import PathSegment, FieldSegment  # noqa: PLC0415

        if not path:
            return MlodyValueValue(struct=value)

        # Parquet-backed values: delegate entire path to ParquetTraversalStrategy
        # before any virtual-value or record-aware processing (D-2, task 4.4).
        location = getattr(value, "location", None)
        if getattr(location, "type", None) == "parquet":
            return ParquetTraversalStrategy().traverse(value, path, label)

        # Check whether the path contains any non-FieldSegment / non-str segments.
        # If it does, we must use the engine-aware loop (tasks 4.1–4.3).
        def _is_field_only(p: tuple[object, ...]) -> bool:
            for seg in p:
                if isinstance(seg, str):
                    continue
                if isinstance(seg, FieldSegment):
                    continue
                return False
            return True

        has_engine_segs = not _is_field_only(path)

        if has_engine_segs or isinstance(value, MlodyVectorValue):
            # Engine-aware loop: handles IndexSegment, KeySegment,
            # WildcardSegment, RecursiveDescentSegment, and mapped traversal
            # over vector accumulators (tasks 4.2–4.3).
            return self._traverse_with_engine(value, path, label, traversal_error_policy)

        # Pure FieldSegment / str path — use the fast record-aware loop.
        # Cast path to tuple[str, ...] for the existing logic.
        str_path = tuple(s.name if isinstance(s, FieldSegment) else s for s in path)  # type: ignore[union-attr]

        value_type = getattr(value, "type", None)
        location = getattr(value, "location", None)
        if (
            isinstance(value, Struct)
            and location is not None
            and getattr(location, "type", None) == "virtual"
        ):
            try:
                child_value = traverse_virtual_value(
                    value,
                    str_path,
                    "'" + ".".join(str_path),
                )
            except (AttributeError, KeyError) as exc:
                missing = str(exc.args[0]) if exc.args else str_path[-1]
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute '{missing}' not found "
                        f"(label: {label!r})"
                    ),
                )
            return MlodyValueValue(struct=child_value)

        _vt_attrs = getattr(value_type, "attributes", None)
        _vt_attrs_fields = _vt_attrs.get("fields") if isinstance(_vt_attrs, dict) else None
        is_record = (
            getattr(value_type, "kind", None) == "record"
            or getattr(value_type, "_root_kind", None) == "record"
            or bool(getattr(value_type, "fields", None) or _vt_attrs_fields)
        )

        if len(str_path) == 1 and is_record:
            result = _traverse_one_step(value, str_path[0], (), label, traversal_error_policy)
            if isinstance(result, MlodyUnresolvedValue):
                return result
            if isinstance(result, MlodyValue):
                return result
            return MlodyValueValue(struct=result[0])

        if len(str_path) >= 2 and is_record:
            current: object = value
            for i, segment in enumerate(str_path):
                step = _traverse_one_step(current, segment, tuple(str_path[:i]), label, traversal_error_policy)
                if isinstance(step, MlodyUnresolvedValue):
                    return step
                if isinstance(step, MlodyValue):
                    # Non-tuple return (engine delegated) — use as-is
                    current = step
                    if i < len(str_path) - 1 and isinstance(step, MlodyUnresolvedValue):
                        return step
                    continue
                rebuilt, _ = step
                # After the first step, ``rebuilt`` is a field struct.  For
                # subsequent steps to use record-aware traversal, the rebuilt
                # struct must itself be record-typed.  If it is not, the spec
                # requires MlodyUnresolvedValue naming the non-record intermediate.
                if i < len(str_path) - 1:
                    next_type = getattr(rebuilt, "type", None)
                    next_is_record = (
                        getattr(next_type, "kind", None) == "record"
                        or getattr(next_type, "_root_kind", None) == "record"
                    )
                    if not next_is_record:
                        json_result = _traverse_json_backed_value(
                            rebuilt,
                            tuple(str_path[i + 1:]),
                            label,
                        )
                        if json_result is not None:
                            return json_result
                        type_kind = getattr(next_type, "kind", "<unknown>")
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"field {segment!r} is not a record type "
                                f"(got {type_kind!r}); cannot traverse further"
                            ),
                        )
                current = rebuilt
            if isinstance(current, MlodyValue):
                return current
            return MlodyValueValue(struct=current)

        json_result = _traverse_json_backed_value(value, str_path, label)
        if json_result is not None:
            return json_result

        # Non-record root or single-segment non-record path: generic getattr
        # traversal.  This is the OQ-13 extension seam — a future per-kind
        # traversal dispatch framework would replace this fallback with a
        # handler registered in a table analogous to _LOCATION_COMPOSERS.
        obj: object = value
        for i, segment in enumerate(str_path):
            try:
                obj = getattr(obj, segment)
            except AttributeError:
                traversed = ".".join(str_path[:i])
                parent = f" on '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute '{segment}' not found"
                        f"{parent} (label: {label!r})"
                    ),
                )
        terminal_kind = getattr(obj, "kind", None)
        if isinstance(terminal_kind, str) and terminal_kind in TRAVERSAL_STRATEGIES:
            return _wrap_struct(terminal_kind, obj)
        return _RawAttrValue(value=obj, label=label)

    def _traverse_with_engine(
        self,
        value: object,
        path: tuple[object, ...],
        label: "Label",
        policy: TraversalErrorPolicy,
    ) -> MlodyValue:
        """Engine-aware multi-step traversal loop for paths containing non-field segments.

        Implements:
        - Mapped traversal (task 4.3): when the accumulator is a MlodyVectorValue
          and the next segment is FieldSegment/IndexSegment/KeySegment, map the step
          over all elements and collect into a flat MlodyVectorValue.
        - Vector-of-vectors (task 4.4): when the accumulator is a MlodyVectorValue
          and the next segment is WildcardSegment/RecursiveDescentSegment, apply
          the expansion to each element and collect into a MlodyVectorValue whose
          elements are themselves MlodyVectorValues (not flattened).
        """
        from mlody.core.traversal_grammar import (  # noqa: PLC0415
            FieldSegment,
            IndexSegment,
            KeySegment,
            SliceSegment,
            WildcardSegment,
            RecursiveDescentSegment,
        )

        # Seed the accumulator
        if isinstance(value, MlodyValue):
            accumulator: MlodyValue = value
        else:
            accumulator = MlodyValueValue(struct=value)

        for seg in path:
            if isinstance(accumulator, MlodyUnresolvedValue):
                # Short-circuit on failure
                return accumulator

            # Mapped traversal applies FieldSegment and KeySegment over each element of
            # a vector accumulator.  IndexSegment is intentionally excluded: [n] on a
            # MlodyVectorValue means "index into this vector", which is handled by
            # _engine_index_step in the else branch, not by element-wise mapping.
            is_mapping_seg = isinstance(seg, (FieldSegment, KeySegment))
            is_expansion_seg = isinstance(seg, (WildcardSegment, RecursiveDescentSegment))

            if isinstance(accumulator, MlodyVectorValue) and is_mapping_seg:
                # Mapped traversal: apply segment to each element, collect flat
                collected: list[MlodyValue] = []
                for elem in accumulator.elements:
                    elem_result = _traverse_one_step(elem, seg, (), label, policy)
                    if isinstance(elem_result, MlodyUnresolvedValue):
                        if policy is TraversalErrorPolicy.RAISE:
                            return elem_result
                        # SKIP: omit this element
                        continue
                    if isinstance(elem_result, MlodyValue):
                        collected.append(elem_result)
                    elif isinstance(elem_result, tuple):
                        rebuilt, _ = elem_result
                        collected.append(MlodyValueValue(struct=rebuilt))
                accumulator = MlodyVectorValue(elements=tuple(collected))

            elif isinstance(accumulator, MlodyVectorValue) and is_expansion_seg:
                # Vector-of-vectors: apply expansion to each element independently
                # (not flattened — hierarchical multi-expansion, spec §multiple wildcards)
                nested: list[MlodyValue] = []
                for elem in accumulator.elements:
                    elem_result = _traverse_one_step(elem, seg, (), label, policy)
                    if isinstance(elem_result, MlodyUnresolvedValue):
                        if policy is TraversalErrorPolicy.RAISE:
                            return elem_result
                        continue
                    if isinstance(elem_result, MlodyValue):
                        nested.append(elem_result)
                    elif isinstance(elem_result, tuple):
                        rebuilt, _ = elem_result
                        nested.append(MlodyValueValue(struct=rebuilt))
                accumulator = MlodyVectorValue(elements=tuple(nested))

            else:
                # Non-vector accumulator or str segment: single step
                step = _traverse_one_step(accumulator, seg, (), label, policy)
                if isinstance(step, MlodyValue):
                    accumulator = step
                elif isinstance(step, tuple):
                    rebuilt, _ = step
                    accumulator = MlodyValueValue(struct=rebuilt)
                else:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=f"unexpected result from _traverse_one_step: {step!r}",
                    )

        return accumulator


# ---------------------------------------------------------------------------
# ParquetTraversalStrategy  (tasks 4.1–4.5, design D-2, D-4)
# ---------------------------------------------------------------------------


class ParquetTraversalStrategy:
    """Traversal strategy for ``kind="value"`` entities backed by a Parquet file.

    Delegates all segment dispatch to ``ParquetDeserializer``:
    - ``IndexSegment(n)``             → ``_read_row(n)``   → ``dict[str, Any]``
    - ``SliceSegment(start,stop,step)`` → ``_read_slice``  → ``list[dict]``
    - ``FieldSegment(name)`` on dict  → ``dict[name]``
    - ``FieldSegment(name)`` on list  → ``[d[name] for d in list]``

    Errors (missing location path, file not found, IndexError, KeyError) are
    soft-failed as ``MlodyUnresolvedValue`` — never propagated to the caller.
    """

    def traverse(
        self,
        value: object,
        path: tuple[object, ...],
        label: "Label",
        **kwargs: object,
    ) -> MlodyValue:
        """Apply *path* to a Parquet-backed value struct.

        Args:
            value: The root Starlark Struct with ``location.type == "parquet"``.
            path:  Combined ``(entity.field_path segments) + (attr_path segments)``
                   tuple of typed ``PathSegment`` objects.
            label: Originating label (used in unresolved reasons).
            **kwargs: Accepted for protocol compatibility (e.g.
                ``traversal_error_policy``); not used by this strategy.

        Returns:
            ``_RawAttrValue`` wrapping the terminal Python value, or
            ``MlodyUnresolvedValue`` on any failure.
        """
        from mlody.core.parquet import ParquetDeserializer  # noqa: PLC0415
        from mlody.core.traversal_grammar import (  # noqa: PLC0415
            FieldSegment,
            IndexSegment,
            KeySegment,
            SliceSegment,
        )

        import glob as _glob  # noqa: PLC0415

        location = getattr(value, "location", None)
        _loc_root_kind = (
            getattr(location, "_root_kind", None) or getattr(location, "type", None)
        )

        # Derived locations must be materialised before we can read rows from them.
        if _loc_root_kind == "derived":
            try:
                from mlody.core.derived import materialise_derived  # noqa: PLC0415

                _attrs = getattr(location, "attributes", {}) or {}
                _source_paths = _attrs.get("source_paths") or []
                if not _source_paths:
                    _source_paths = str(_attrs.get("source_ref") or "")
                path_val: object = materialise_derived(location, _source_paths)
            except Exception as exc:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"Failed to materialise derived location for parquet traversal: "
                        f"{exc} (label: {label!r})"
                    ),
                )
        else:
            path_val: object = getattr(location, "path", None)
            if path_val is None:
                _loc_attrs = getattr(location, "attributes", None)
                if isinstance(_loc_attrs, dict):
                    path_val = _loc_attrs.get("path")
            if path_val is None:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        "Parquet traversal requires a location with a 'path' attribute; "
                        f"got location {location!r} (label: {label!r})"
                    ),
                )

        # Resolve glob patterns and lists to concrete file paths (sorted).
        if isinstance(path_val, (list, tuple)):
            file_paths: list[str] = [os.path.expanduser(str(p)) for p in path_val if str(p)]
        else:
            _expanded = os.path.expanduser(str(path_val))
            if _glob.has_magic(_expanded):
                file_paths = sorted(_glob.glob(_expanded))
            else:
                file_paths = [_expanded]

        if not file_paths:
            return MlodyUnresolvedValue(
                label=label,
                reason=f"No parquet files found at {path_val!r} (label: {label!r})",
            )

        # Apply each path segment left-to-right, feeding each step's output
        # as the input to the next step (chained traversal, D-4).
        # current starts as a list[str] of file paths; becomes a dict (row) after
        # an IndexSegment, or a list[dict] after a SliceSegment.
        current: object = file_paths
        for seg in path:
            if isinstance(current, list) and current and isinstance(current[0], str):
                # File-paths list: dispatch IndexSegment/SliceSegment to read rows.
                if isinstance(seg, IndexSegment):
                    idx = seg.index
                    # Open deserializers and normalise negative index.
                    _desers: list[ParquetDeserializer] = []
                    for fp in current:
                        try:
                            _desers.append(ParquetDeserializer(fp))
                        except FileNotFoundError as exc:
                            return MlodyUnresolvedValue(
                                label=label,
                                reason=f"Parquet file not found: {fp!r} — {exc} (label: {label!r})",
                            )
                    if idx < 0:
                        _total = sum(d.num_rows for d in _desers)
                        idx = _total + idx
                    _cumulative = 0
                    _found: dict | None = None
                    for _d in _desers:
                        _n = _d.num_rows
                        if idx < _cumulative + _n:
                            try:
                                _found = _d[idx - _cumulative]
                            except IndexError as exc:
                                return MlodyUnresolvedValue(
                                    label=label,
                                    reason=f"Parquet index error: {exc} (label: {label!r})",
                                )
                            break
                        _cumulative += _n
                    if _found is None:
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"Parquet index {seg.index!r} out of range "
                                f"(label: {label!r})"
                            ),
                        )
                    current = _found
                elif isinstance(seg, SliceSegment):
                    from mlody.core.parquet import read_file_as_rows  # noqa: PLC0415

                    _all_rows: list[dict] = []
                    for fp in current:
                        try:
                            _all_rows.extend(read_file_as_rows(fp))
                        except Exception as exc:
                            return MlodyUnresolvedValue(
                                label=label,
                                reason=f"Error reading {fp!r}: {exc} (label: {label!r})",
                            )
                    current = _all_rows[seg.start : seg.stop : seg.step]
                elif isinstance(seg, FieldSegment):
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"FieldSegment {seg.name!r} applied directly to Parquet files "
                            f"without a preceding row index (label: {label!r})"
                        ),
                    )
                else:
                    from mlody.core.traversal_grammar import SqlSegment  # noqa: PLC0415
                    if isinstance(seg, SqlSegment):
                        from mlody.core.sql import MlodyQueryError, mlody_query  # noqa: PLC0415
                        try:
                            table = mlody_query(paths=current, query=seg.query)
                        except MlodyQueryError as exc:
                            return MlodyUnresolvedValue(
                                label=label,
                                reason=f"SQL query failed: {exc} (label: {label!r})",
                            )
                        return _RawAttrValue(value=table, label=label)
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"unsupported path segment {type(seg).__name__!r} "
                            f"on Parquet file list (label: {label!r})"
                        ),
                    )
            elif isinstance(current, ParquetDeserializer):
                # Single-file deserializer (legacy / direct use).
                if isinstance(seg, IndexSegment):
                    try:
                        current = current[seg.index]
                    except IndexError as exc:
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=f"Parquet index error: {exc} (label: {label!r})",
                        )
                elif isinstance(seg, SliceSegment):
                    current = current[seg.start : seg.stop : seg.step]
                elif isinstance(seg, FieldSegment):
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"FieldSegment {seg.name!r} applied directly to Parquet file "
                            f"without a preceding row index (label: {label!r})"
                        ),
                    )
                else:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"unsupported path segment {type(seg).__name__!r} "
                            f"on Parquet deserializer (label: {label!r})"
                        ),
                    )
            elif isinstance(current, dict):
                if isinstance(seg, (FieldSegment, KeySegment)):
                    key = seg.name if isinstance(seg, FieldSegment) else seg.key
                    if key not in current:
                        available = list(current.keys())
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"column {key!r} not found in row; "
                                f"available columns: {available} (label: {label!r})"
                            ),
                        )
                    current = current[key]
                else:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"unsupported path segment {type(seg).__name__!r} "
                            f"on row dict (label: {label!r})"
                        ),
                    )
            elif isinstance(current, list):
                if isinstance(seg, (FieldSegment, KeySegment)):
                    # Mapped traversal: extract the named key from each row dict.
                    _key = seg.name if isinstance(seg, FieldSegment) else seg.key
                    try:
                        current = [row[_key] for row in current]  # type: ignore[index]
                    except KeyError:
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"column {_key!r} not found in one or more rows "
                                f"(label: {label!r})"
                            ),
                        )
                elif isinstance(seg, IndexSegment):
                    try:
                        current = current[seg.index]
                    except IndexError as exc:
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=f"index error on slice result: {exc} (label: {label!r})",
                        )
                else:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"unsupported path segment {type(seg).__name__!r} "
                            f"on list-of-rows (label: {label!r})"
                        ),
                    )
            else:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"cannot apply path segment {type(seg).__name__!r} "
                        f"to value of type {type(current).__name__!r} (label: {label!r})"
                    ),
                )

        # Wrap terminal result in _RawAttrValue (spec: strategy returns _RawAttrValue).
        # If path was empty we wrap the deserializer itself (unusual but valid).
        return _RawAttrValue(value=current, label=label)


# ---------------------------------------------------------------------------
# Dispatch table  (task 2.3)
# ---------------------------------------------------------------------------

TRAVERSAL_STRATEGIES: dict[str, TraversalStrategy] = {
    "task": StructTraversalStrategy("task"),
    "action": StructTraversalStrategy("action"),
    "value": ValueTraversalStrategy(),
}


# ---------------------------------------------------------------------------
# Entity lookup  (task 3.2)
# ---------------------------------------------------------------------------


def _lookup_entity(
    workspace: "Workspace",
    stem: str,
    name: str,
) -> tuple[str, object] | None:
    """Scan ``workspace.evaluator.all`` for ``(kind, stem, name)``.

    Returns ``(kind, struct)`` on the first match, or ``None`` if not found.

    The registry key shape ``(kind, stem, name)`` is documented in
    ``starlarkish/evaluator/evaluator.py`` and used by ``workspace.resolve()``.
    Coupling note: see design.md §R-002 for the accepted trade-off.
    """
    for key, value in workspace.evaluator.all.items():
        if (
            isinstance(key, tuple)
            and len(key) == 3
            and key[1] == stem
            and key[2] == name
        ):
            return (key[0], value)
    return None


# ---------------------------------------------------------------------------
# Resolver  (tasks 3.1, 3.3, 3.4)
# ---------------------------------------------------------------------------


def resolve_label_to_value(
    label: "Label",
    workspace: "Workspace",
    *,
    traversal_error_policy: TraversalErrorPolicy = TraversalErrorPolicy.RAISE,
) -> MlodyValue:
    """Resolve a concrete ``Label`` to a typed ``MlodyValue``.

    Accepts only non-wildcard labels.  Wildcard expansion is the caller's
    responsibility and MUST happen before calling this function.

    Resolution pipeline (design §Resolution Pipeline):
    1. Derive absolute path from workspace root + root path + entity path.
    2. Terminal filesystem check: directory → MlodyFolderValue;
       ``<path>.mlody`` → MlodySourceValue or entity lookup.
    3. Entity name present: scan evaluator registry; dispatch to strategy table.
    4. Attribute path present on folder/source: MlodyUnresolvedValue.
    5. Any step fails: MlodyUnresolvedValue with step-specific reason.

    Args:
        label: The concrete (non-wildcard) label to resolve.
        workspace: The loaded workspace to resolve against.
        traversal_error_policy: Controls SKIP/RAISE behaviour for traversal
            steps that cannot proceed (missing field, out-of-bounds index,
            type mismatch).  Defaults to RAISE for backward compatibility.

    Raises:
        ValueError: if ``label.entity`` is a wildcard (programmer error).
    """
    # Guard: wildcard labels must be expanded before calling this function.
    if label.entity is not None and label.entity.wildcard:
        raise ValueError(
            f"resolve_label_to_value received a wildcard label {label!r}; "
            "expand wildcards before calling this function"
        )

    # -----------------------------------------------------------------------
    # Workspace-level label: no entity spec
    # -----------------------------------------------------------------------
    # When no entity is specified, the attribute path (if present) is treated
    # as a filesystem path relative to the monorepo root ("root substitution"):
    #   'info  →  <monorepo_root>/info  →  MlodyFolderValue or MlodySourceValue
    # A bare workspace label with no path at all → MlodyWorkspaceValue.
    if label.entity is None:
        if label.attribute_path is not None:
            from mlody.core.virtual_value import make_virtual_value  # noqa: PLC0415

            ws_type = workspace.evaluator._types_by_name.get("mlody-workspace")  # type: ignore[attr-defined]
            if ws_type is None:
                return MlodyUnresolvedValue(
                    label=label,
                    reason="type 'mlody-workspace' is not registered",
                )

            label_str = "'" + ".".join(label.attribute_path)

            def _workspace_materializer(_v: object) -> object:
                return workspace

            root_value = make_virtual_value(
                value_type=ws_type,
                label=label_str,
                materializer=_workspace_materializer,
            )
            return ValueTraversalStrategy().traverse(root_value, label.attribute_path, label)
        return MlodyWorkspaceValue(
            name=label.workspace,
            root=str(workspace._monorepo_root),  # noqa: SLF001
        )

    # -----------------------------------------------------------------------
    # Step 1: derive absolute path
    # -----------------------------------------------------------------------
    entity_path: str = ""
    if label.entity is not None and label.entity.path:
        entity_path = label.entity.path.lstrip("/").rstrip("/")

    root_path: str = ""
    if label.entity is not None and label.entity.root is not None:
        root_info = workspace.root_infos.get(label.entity.root)
        if root_info is None:
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"root '{label.entity.root}' not found in workspace; "
                    f"available roots: {sorted(workspace.root_infos.keys())}"
                ),
            )
        root_path = root_info.path.lstrip("/").rstrip("/")

    # Bare root reference (@lexica with no path/name) → MlodyFolderValue for the
    # root directory, since a named root maps to a folder on disk.
    if label.entity is not None and label.entity.path is None and label.entity.name is None:
        root_abs = workspace._monorepo_root / root_path if root_path else workspace._monorepo_root  # noqa: SLF001
        children = sorted(os.listdir(root_abs))
        return MlodyFolderValue(path=root_path, children=children)

    # Build the absolute path: monorepo_root / root_path / entity_path
    abs_path = workspace._monorepo_root  # noqa: SLF001 — accepted per design R-002
    if root_path:
        abs_path = abs_path / root_path
    if entity_path:
        abs_path = abs_path / entity_path

    # -----------------------------------------------------------------------
    # Step 2: terminal filesystem classification
    # -----------------------------------------------------------------------
    entity_name: str | None = None
    if label.entity is not None:
        entity_name = label.entity.name

    attr_path: tuple[str, ...] | None = label.attribute_path

    # TODO(mlody-label-traversal): uniform-level-traversal — when workspace/folder
    # level traversal is extended, MlodyFolderValue.children could be treated as a
    # vector here and wildcard/recursive-descent segments applied before the `:` boundary.
    # See design.md §D-6 for the extension plan.
    if abs_path.is_dir():
        # Folder — entity name on a folder is not supported in v1
        if entity_name is not None:
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"entity name '{entity_name}' specified on a folder "
                    f"'{entity_path}'; use a .mlody source file path to address entities"
                ),
            )
        if attr_path is not None:
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"attribute traversal is not supported for folder values "
                    f"(label: {label!r})"
                ),
            )
        children = sorted(os.listdir(abs_path))
        return MlodyFolderValue(path=entity_path, children=children)

    # Check for a .mlody source file (suffix never in the label)
    mlody_path = abs_path.parent / (abs_path.name + ".mlody")

    if mlody_path.exists():
        # Source file found. If no entity name, return MlodySourceValue.
        if entity_name is None:
            if attr_path is not None:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute traversal is not supported for source-file values "
                        f"(label: {label!r})"
                    ),
                )
            return MlodySourceValue(path=entity_path)

        # -----------------------------------------------------------------------
        # Step 3: entity lookup
        # -----------------------------------------------------------------------
        # Derive stem: root_path / entity_path (mirrors evaluator._register logic)
        stem_parts: list[str] = []
        if root_path:
            stem_parts.append(root_path)
        if entity_path:
            stem_parts.append(entity_path)
        stem = "/".join(stem_parts)

        lookup = _lookup_entity(workspace, stem, entity_name)
        if lookup is None:
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"entity '{entity_name}' not found in registry "
                    f"(stem: '{stem}', label: {label!r})"
                ),
            )

        kind, struct = lookup

        # -----------------------------------------------------------------------
        # Step 4 / 5: attribute-path traversal via dispatch table
        # -----------------------------------------------------------------------
        strategy = TRAVERSAL_STRATEGIES.get(kind)
        if strategy is None:
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    f"kind '{kind}' is not supported by the label-value resolver "
                    f"(label: {label!r})"
                ),
            )

        # Combine entity field_path (from the colon section, e.g. ":task.out.w")
        # with the tick attribute_path (from "'out.w") into one traversal sequence.
        entity_field_path: tuple[str, ...] = (
            label.entity.field_path if label.entity and label.entity.field_path else ()
        )
        attr_path_tuple: tuple[str, ...] = attr_path if attr_path is not None else ()
        resolved_path: tuple[str, ...] = entity_field_path + attr_path_tuple

        # For Parquet-backed entities, convert plain string segments to typed
        # FieldSegment instances so that ParquetTraversalStrategy can dispatch
        # correctly (task 4.5, spec §PathSegment types forwarded).
        location_of_struct = getattr(struct, "location", None)
        if getattr(location_of_struct, "type", None) == "parquet":
            from mlody.core.traversal_grammar import FieldSegment as _FS  # noqa: PLC0415

            parquet_path: tuple[object, ...] = tuple(
                _FS(name=s) for s in resolved_path
            )
            result: MlodyValue = strategy.traverse(  # type: ignore[call-arg]
                struct,
                parquet_path,
                label,
                traversal_error_policy=traversal_error_policy,
            )
        else:
            # Pass traversal_error_policy through to the strategy.  ValueTraversalStrategy
            # acts on it (design D-4); StructTraversalStrategy accepts but ignores it via
            # **kwargs (pure getattr traversal has no SKIP semantics).
            result = strategy.traverse(  # type: ignore[call-arg]
                struct,
                resolved_path,
                label,
                traversal_error_policy=traversal_error_policy,
            )

        # Apply entity_query (e.g. [1], ["key"], [*]) as a post-step after the
        # field-path traversal.  The label parser strips brackets and stores the
        # inner content, so we reconstruct "[query]" for the traversal parser.
        if label.entity_query is not None and not isinstance(result, MlodyUnresolvedValue):
            from mlody.core.traversal_parser import (  # noqa: PLC0415
                TraversalParseError,
                parse_traversal_expression,
            )
            try:
                eq_expr = parse_traversal_expression(f"[{label.entity_query}]")
            except TraversalParseError:
                eq_expr = None
            if eq_expr is not None and eq_expr.segments:
                seg = eq_expr.segments[0]
                # For Parquet-backed entities the entity_query bracket expression
                # (e.g. [0]) must be forwarded through ParquetTraversalStrategy
                # rather than the generic _traverse_one_step, because the strategy
                # carries the file path needed to read from disk.
                # Prefer checking the traversal RESULT for parquet backing — the
                # root struct may have a posix location while a nested field is
                # the actual parquet-backed vector (e.g. celebA-dataset.valid[1]).
                _result_struct: object = None
                if isinstance(result, MlodyValueValue):
                    _result_struct = result.struct
                if _result_struct is not None and _is_parquet_backed(_result_struct):
                    from mlody.core.traversal_grammar import PathSegment  # noqa: PLC0415
                    all_pq_segs = tuple(
                        s for s in eq_expr.segments if isinstance(s, PathSegment)
                    )
                    if all_pq_segs:
                        pq_result = ParquetTraversalStrategy().traverse(
                            _result_struct,
                            all_pq_segs,
                            label,
                        )
                        return pq_result
                elif getattr(location_of_struct, "type", None) == "parquet":
                    from mlody.core.traversal_grammar import PathSegment  # noqa: PLC0415
                    all_pq_segs = tuple(
                        s for s in eq_expr.segments if isinstance(s, PathSegment)
                    )
                    if all_pq_segs:
                        pq_result = ParquetTraversalStrategy().traverse(
                            struct,
                            all_pq_segs,
                            label,
                        )
                        return pq_result
                step = _traverse_one_step(result, seg, resolved_path, label, traversal_error_policy)
                if isinstance(step, MlodyUnresolvedValue):
                    return step
                if isinstance(step, MlodyValue):
                    return step
                if isinstance(step, tuple):
                    return MlodyValueValue(struct=step[0])

        return result

    # Neither a directory nor a .mlody source file
    return MlodyUnresolvedValue(
        label=label,
        reason=(
            f"path '{entity_path}' is not a directory or .mlody source file "
            f"under '{workspace._monorepo_root}' (label: {label!r})"  # noqa: SLF001
        ),
    )
