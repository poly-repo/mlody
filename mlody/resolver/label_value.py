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
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Sequence, Union
from rich.pretty import pretty_repr

from common.python.console import RichDomNode, SyntaxNode, StackNode, text, fragment, panel, md, tree, table, stack

if TYPE_CHECKING:
    import pyarrow as pa
    import pyarrow.parquet as _pyarrow_parquet
    from mlody.core.derived import materialise_derived
    from mlody.core.parquet import ParquetDeserializer, read_file_as_rows
    from mlody.core.label.label import Label
    from mlody.core.sql.sql_query import MlodyQueryError, mlody_query
    from mlody.core.tabular.location_specs import DerivedLocationSpec
    from mlody.core.workspace import Workspace

from mlody.core.type_display import format_type_label


# ---------------------------------------------------------------------------
# Arrow → mlody type mapping  (FR-002, spec §4)
# ---------------------------------------------------------------------------
#
# Populated at import time. ``pa.DataType.__eq__`` compares by value so the
# dict lookup works correctly.  Types not in this table cause promotion to
# return ``MlodyUnresolvedValue`` rather than a bare ``_RawAttrValue``.
#
# Arrow temporal and binary types (date32, timestamp, binary, etc.) are
# explicitly out of scope for this change (FR-002 §4).

def _build_arrow_type_map() -> "dict[pa.DataType, str]":
    """Build the Arrow-to-mlody-type-name mapping dict."""
    import pyarrow as _pa  # noqa: PLC0415

    return {
        _pa.bool_(): "bool",
        _pa.int8(): "integer",
        _pa.int16(): "integer",
        _pa.int32(): "integer",
        _pa.int64(): "integer",
        _pa.uint8(): "integer",
        _pa.uint16(): "integer",
        _pa.uint32(): "integer",
        _pa.uint64(): "integer",
        _pa.float16(): "float",
        _pa.float32(): "float",
        _pa.float64(): "float",
        _pa.string(): "string",
        _pa.large_string(): "string",
    }


# Lazily initialised on first call to avoid importing pyarrow at module level
# on codepaths that never touch Parquet (e.g. pure-JSON or struct traversal).
_ARROW_TO_MLODY_TYPE_NAME: "dict[pa.DataType, str] | None" = None


def _get_arrow_type_map() -> "dict[pa.DataType, str]":
    """Return the singleton Arrow-to-mlody-type-name mapping, building it on first call."""
    global _ARROW_TO_MLODY_TYPE_NAME  # noqa: PLW0603
    if _ARROW_TO_MLODY_TYPE_NAME is None:
        _ARROW_TO_MLODY_TYPE_NAME = _build_arrow_type_map()
    return _ARROW_TO_MLODY_TYPE_NAME


# ---------------------------------------------------------------------------
# Mlody primitive type struct cache  (spec §6.5)
# ---------------------------------------------------------------------------
#
# These are minimal stubs — they carry enough shape for downstream type
# inspection (``kind``, ``type``, ``name``) but do not include the full
# validator functions from live DSL evaluation.

_MLODY_PRIMITIVE_TYPE_STRUCTS: dict[str, object] = {}


def _get_mlody_primitive_type(name: str) -> object:
    """Return the canonical mlody type struct for a primitive type name.

    Constructs and caches a minimal Struct on first call.  The returned struct
    has ``kind="type"``, ``type=name``, ``name=name``, ``attributes={}``, and
    ``_allowed_attrs={}``.  This is sufficient for downstream type inspection
    and for building the ``vector(element_type=T)`` wrapper struct.

    Args:
        name: One of ``"bool"``, ``"integer"``, ``"float"``, ``"string"``.

    Returns:
        A cached Struct representing the named mlody primitive type.
    """
    if name not in _MLODY_PRIMITIVE_TYPE_STRUCTS:
        from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

        _root_kind_map = {
            "bool": "bool",
            "integer": "integer",
            "float": "float",
            # string inherits from aggregate in the real DSL (spec §3.3 table)
            "string": "aggregate",
        }
        _MLODY_PRIMITIVE_TYPE_STRUCTS[name] = _Struct(
            kind="type",
            type=name,
            name=name,
            _root_kind=_root_kind_map.get(name, name),
            attributes={},
            _allowed_attrs={},
        )
    return _MLODY_PRIMITIVE_TYPE_STRUCTS[name]


# Python-type-to-mlody-type-name mapping for JSON-backed traversal (FR-007).
# ``bool`` is checked before ``int`` because Python ``bool`` is a subclass of ``int``.
_PYTHON_TYPE_TO_MLODY_NAME: list[tuple[type, str]] = [
    (bool, "bool"),
    (int, "integer"),
    (float, "float"),
    (str, "string"),
]


# ---------------------------------------------------------------------------
# Value type hierarchy  (tasks 1.1 – 1.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MlodyValue:
    """Base class for all resolved mlody values."""

    def to_console_representation(self) -> RichDomNode:
        return text("MlodyValue::to_console_representation")


@dataclass(frozen=True)
class MlodyWorkspaceValue(MlodyValue):
    """The workspace itself (label has no entity spec).

    ``name`` is the workspace name (``label.workspace``), or ``None`` for CWD.
    ``root`` is the absolute filesystem path of the monorepo root.
    """

    name: str | None
    root: str

    def to_console_representation(self) -> RichDomNode:
        name = self.name or "(cwd)"
        return panel(
            text(f"root: {self.root}"),
            title=f"workspace: {name}",
            border_style="blue",
        )


@dataclass(frozen=True)
class MlodyFolderValue(MlodyValue):
    """A directory on disk under the workspace.

    ``path`` is workspace-relative (without leading slash), matching the label.
    ``children`` contains the names of the immediate directory entries.
    """

    path: str
    children: list[str]  # pyright: ignore[reportMutableClassVariable]

    def to_console_representation(self) -> RichDomNode:
        child_nodes = [text(c) for c in self.children] if self.children else [text("(empty)")]
        return tree(f"folder: {self.path}", child_nodes)


@dataclass(frozen=True)
class MlodySourceValue(MlodyValue):
    """A ``.mlody`` source file on disk.

    ``path`` is the workspace-relative path **without** the ``.mlody`` suffix,
    matching the label exactly.
    ``abs_path`` is the absolute filesystem path to the ``.mlody`` file,
    used for content display.
    """

    path: str
    abs_path: Path | None = None

    def to_console_representation(self) -> RichDomNode:
        title = f"source: {self.path}.mlody"
        if self.abs_path is not None:
            try:
                content = self.abs_path.read_text()
                return panel(SyntaxNode(content, language="python"), title=title, border_style="green")
            except Exception:
                pass
        return panel(text(self.path + ".mlody"), title=title, border_style="green")


@dataclass(frozen=True)
class MlodyTaskValue(MlodyValue):
    """Opaque wrapper around a task registry Struct."""

    struct: object

    def to_console_representation(self) -> RichDomNode:
        s = self.struct
        name = getattr(s, "name", "?")

        io_cols = ["name", "type", "source", "default"]
        cfg_cols = ["name", "type", "source", "value"]

        nodes: list[RichDomNode] = []

        input_rows = _value_rows(getattr(s, "inputs", None))
        if input_rows:
            nodes.append(table(io_cols, input_rows, title="inputs"))

        output_rows = _value_rows(getattr(s, "outputs", None))
        if output_rows:
            nodes.append(table(io_cols, output_rows, title="outputs"))

        config_rows = _value_rows(getattr(s, "config", None))
        if config_rows:
            nodes.append(table(cfg_cols, config_rows, title="config"))

        return panel(stack(*nodes) if nodes else text("(empty)"), title=f"task: {name}")


@dataclass(frozen=True)
class MlodyActionValue(MlodyValue):
    """Opaque wrapper around an action registry Struct."""

    struct: object

    def to_console_representation(self) -> RichDomNode:
        s = self.struct
        name = getattr(s, "name", "?")

        io_cols = ["name", "type", "source", "default"]
        cfg_cols = ["name", "type", "source", "value"]

        nodes: list[RichDomNode] = []

        input_rows = _value_rows(getattr(s, "inputs", None))
        if input_rows:
            nodes.append(table(io_cols, input_rows, title="inputs"))

        output_rows = _value_rows(getattr(s, "outputs", None))
        if output_rows:
            nodes.append(table(io_cols, output_rows, title="outputs"))

        config_rows = _value_rows(getattr(s, "config", None))
        if config_rows:
            nodes.append(table(cfg_cols, config_rows, title="config"))

        return panel(stack(*nodes) if nodes else text("(empty)"), title=f"action: {name}")


@dataclass(frozen=True)
class MlodyValueValue(MlodyValue):
    """Opaque wrapper around a value registry Struct."""

    struct: object

    def to_console_representation(self) -> RichDomNode:
        content = pretty_repr(_to_display_dict(self.struct), max_width=88)
        return panel(SyntaxNode(content, language="python"), title="value")


@dataclass(frozen=True)
class MlodyUnresolvedValue(MlodyValue):
    """Soft-failure sentinel.

    Returned (never raised) when any resolution step cannot proceed.
    ``reason`` is a human-readable string naming the failed step.
    """

    label: "Label"
    reason: str

    def to_console_representation(self) -> RichDomNode:
        return panel(
            text(self.reason),
            title=f"unresolved: {self.label!r}",
            border_style="red",
        )


@dataclass(frozen=True)
class MlodyVectorValue(MlodyValue):
    """A collection of ``MlodyValue`` elements produced by wildcard or recursive-descent traversal.

    ``elements`` is a tuple of ``MlodyValue`` instances in deterministic order
    (declaration order for wildcards, depth-first for recursive descent).
    """

    elements: tuple[MlodyValue, ...]


@dataclass(frozen=True)
class MlodySourceRangeValue(MlodyValue):
    """A resolved source-range attribute: file path + line span."""

    filepath: str
    abs_path: Path
    start_line: int
    end_line: int

    def to_console_representation(self) -> RichDomNode:
        line_range = f"{self.start_line}...{self.end_line}"
        info_table = table(["path", "lines"], [[text(self.filepath), text(line_range)]])
        try:
            lines = self.abs_path.read_text().splitlines()
            snippet = "\n".join(lines[self.start_line - 1 : self.end_line])
            code: RichDomNode = SyntaxNode(snippet, language="python")
        except Exception:
            code = text(f"(could not read {self.abs_path})")
        return stack(info_table, code)


# ---------------------------------------------------------------------------
# Rendering helpers shared by value-type to_console_representation() methods
# ---------------------------------------------------------------------------


def _to_display_dict(obj: object) -> object:
    """Recursively convert a Starlark Struct to a plain Python dict for display.

    Replaces callables (validator functions, etc.) with a '<function>' placeholder
    so the result is safely passable to pretty_repr for indented rendering.
    """
    if hasattr(obj, "as_mapping"):
        return {k: _to_display_dict(v) for k, v in obj.as_mapping().items()}
    if isinstance(obj, dict):
        return {k: _to_display_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_to_display_dict(v) for v in obj)
    if callable(obj) and not isinstance(obj, type):
        return "<function>"
    return obj


def _fmt_type(t: object) -> str:
    return format_type_label(t)


def _fmt_location(loc: object) -> str:
    if loc is None:
        return "-"
    return str(getattr(loc, "type", "-"))


def _fmt_default(v: object) -> str:
    return "-" if v is None else str(v)


def _value_rows(container: object) -> list[list[RichDomNode]]:
    """Return table rows for a struct of value-structs (inputs/outputs/config)."""
    if container is None or not hasattr(container, "as_mapping"):
        return []
    rows: list[list[RichDomNode]] = []
    for k, v in container.as_mapping().items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        rows.append([
            text(str(getattr(v, "name", k))),
            text(_fmt_type(getattr(v, "type", None))),
            text(_fmt_location(getattr(v, "location", None))),
            text(_fmt_default(getattr(v, "default", None))),
        ])
    return rows


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
        path: "tuple[PathSegment, ...]",
        label: "Label",
        **kwargs: object,  # Accepts traversal_error_policy for protocol compatibility (D-4)
    ) -> MlodyValue:
        from mlody.core.traversal_grammar import FieldSegment, PathSegment  # noqa: PLC0415
        from mlody.core.traversal_runtime import step_named_child  # noqa: PLC0415
        from mlody.core.virtual_value import (  # noqa: PLC0415
            lookup_runtime_attribute,
            synthesize_runtime_child,
        )

        if not path:
            return _wrap_struct(self._kind, value)

        obj: object = value
        parent_obj: object = value  # tracks the object before the last getattr step
        terminal_field_name: str | None = None
        _sentinel = object()
        for i, segment in enumerate(path):
            parent_obj = obj
            if isinstance(segment, FieldSegment):
                field_name = segment.name
            elif isinstance(segment, str):
                # Back-compat: callers that still pass raw strings.
                field_name = segment
            else:
                traversed = "".join(str(s) for s in path[:i])
                parent = f" on '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"segment {type(segment).__name__} is not supported on "
                        f"struct-kind values{parent} (label: {label!r})"
                    ),
                )
            try:
                obj = step_named_child(obj, field_name)
            except (AttributeError, KeyError):
                field_decl = lookup_runtime_attribute(obj, field_name)
                if field_decl is not None:
                    raw_value = getattr(obj, field_name, _sentinel)
                    if raw_value is not _sentinel:
                        obj = raw_value
                    else:
                        synthesized = synthesize_runtime_child(obj, field_name)
                        if synthesized is not None:
                            obj = synthesized
                        else:
                            field_decl = None
                if field_decl is not None:
                    terminal_field_name = field_name
                    continue
                traversed = "".join(str(s) for s in path[:i])
                parent = f" on '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute '{field_name}' not found{parent} (label: {label!r})"
                    ),
                )
            terminal_field_name = field_name
        # Terminal value reached — if it's a known entity kind (e.g. the .action
        # field on a task struct is itself an action), wrap it properly so the
        # caller gets a typed MlodyValue rather than a raw dump.
        terminal_kind = getattr(obj, "kind", None)
        if isinstance(terminal_kind, str) and terminal_kind in TRAVERSAL_STRATEGIES:
            return _wrap_struct(terminal_kind, obj)

        # FR-008 / spec §6.4: attempt scalar promotion for Starlark struct fields
        # when the terminal value is a scalar AND the parent struct declares a
        # mlody field type for the terminal segment.  No promotion without a
        # declaration — return _RawAttrValue unchanged.
        if isinstance(obj, (bool, int, float, str)) and terminal_field_name is not None:
            _parent_type = getattr(parent_obj, "type", None)
            _direct_fields = getattr(_parent_type, "fields", None)
            _attrs_dict = getattr(_parent_type, "attributes", None)
            _attrs_fields = (
                _attrs_dict.get("fields") if isinstance(_attrs_dict, dict) else None
            )
            _fields_list: list[object] = list(_direct_fields or _attrs_fields or [])
            _field_decl: object = None
            for _f in _fields_list:
                if getattr(_f, "name", None) == terminal_field_name:
                    _field_decl = _f
                    break
            if _field_decl is None:
                _field_decl = lookup_runtime_attribute(parent_obj, terminal_field_name)
            if _field_decl is not None:
                _declared_type = getattr(_field_decl, "type", None)
                if _declared_type is not None:
                    _promoted = promote_scalar_leaf(
                        obj, terminal_field_name, _declared_type, label
                    )
                    if _promoted is not None:
                        return _promoted

        # Promote a list/tuple of homogeneous known-kind entity structs to MlodyVectorValue.
        # An empty list also promotes (empty vector).
        if isinstance(obj, (list, tuple)):
            elements = tuple(
                _wrap_struct(getattr(item, "kind"), item)
                for item in obj
                if isinstance(getattr(item, "kind", None), str)
                and getattr(item, "kind", None) in TRAVERSAL_STRATEGIES
            )
            if len(elements) == len(obj):
                return MlodyVectorValue(elements=elements)

        # Promote a struct-of-values to MlodyVectorValue.
        # After workspace loading, _convert_ports_to_structs transforms port lists
        # into Struct(name→entity_struct) objects.  If every field value is a
        # known-kind entity struct (or the struct is empty), treat as a vector.
        from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415
        if isinstance(obj, _Struct):
            field_values = list(obj.as_mapping().values())
            if all(
                isinstance(getattr(fv, "kind", None), str)
                and getattr(fv, "kind", None) in TRAVERSAL_STRATEGIES
                for fv in field_values
            ):
                return MlodyVectorValue(
                    elements=tuple(
                        _wrap_struct(getattr(fv, "kind"), fv) for fv in field_values
                    )
                )

        return _RawAttrValue(value=obj, label=label)


@dataclass(frozen=True)
class _RawAttrValue(MlodyValue):
    """Internal: terminal value reached after attribute-path traversal."""

    value: object
    label: "Label"

    def to_console_representation(self) -> RichDomNode:
        return text(str(self.value))


# ---------------------------------------------------------------------------
# Scalar promotion helper  (FR-001 – FR-005, FR-009, spec §5)
# ---------------------------------------------------------------------------


def promote_scalar_leaf(
    scalars: object,
    field_name: str,
    element_type: object,
    label: "Label",
    *,
    register: bool = False,
    evaluator: object | None = None,
) -> "MlodyValueValue | MlodyUnresolvedValue | None":
    """Promote a scalar (or homogeneous list of scalars) to a ``MlodyValueValue``.

    Builds a ``kind="value"`` Struct with an ``inline`` location carrying the
    data as a Python tuple, and a ``vector(element_type=T)`` type struct.  The
    result is a first-class mlody value — typed, location-carrying, and
    optionally registerable in the evaluator.

    Args:
        scalars: The raw value(s) to promote.  Accepted shapes: a single
            ``bool``, ``int``, ``float``, or ``str``; or a ``list`` / ``tuple``
            of same.  Any other shape causes the function to return ``None``.
        field_name: The bare field name string, used as the ``name`` field on
            the promoted struct and in error messages.
        element_type: The resolved mlody type struct to use as ``element_type``
            inside the ``vector(...)`` wrapper.  Must already be the correct
            struct — this function does not perform Arrow mapping.
        label: The originating ``Label``, threaded into ``MlodyUnresolvedValue``.
        register: When ``True``, register the promoted value with the evaluator.
            Raises ``NotImplementedError`` in this release (OQ-003 deferred).
        evaluator: Required when ``register=True``; ignored otherwise.

    Returns:
        ``MlodyValueValue`` on success, ``MlodyUnresolvedValue`` on failure,
        or ``None`` when ``scalars`` does not qualify for promotion (caller
        should return the original ``_RawAttrValue`` unchanged).
    """
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    _SCALAR_TYPES = (bool, int, float, str)

    # Normalise to tuple (FR-004: data is always a Python tuple).
    if isinstance(scalars, _SCALAR_TYPES):
        data: tuple[object, ...] = (scalars,)
    elif isinstance(scalars, (list, tuple)):
        # Must be homogeneous scalars, not dicts or nested structures (FR-010).
        if not scalars or not all(isinstance(v, _SCALAR_TYPES) for v in scalars):
            return None
        data = tuple(scalars)
    else:
        return None

    # Build inline location struct (FR-009, spec §3.2).
    location_struct = _Struct(
        kind="location",
        type="inline",
        name="inline",
        data=data,
        abstract=False,
        _root_kind="inline",
        attributes={"data": data},
        _allowed_attrs={},
    )

    # Build vector type struct (spec §3.3).
    vector_type_struct = _Struct(
        kind="type",
        type="vector",
        name="vector",
        _root_kind="aggregate",
        attributes={"element_type": element_type},
        _allowed_attrs={},
    )

    # Build the promoted value struct (FR-004, spec §3.1).
    promoted_struct = _Struct(
        kind="value",
        name=field_name,
        type=vector_type_struct,
        location=location_struct,
        _lineage=[],
    )

    if register:
        # OQ-003: stem selection is deferred; raise NotImplementedError rather
        # than silently ignoring the flag so future callers get a clear signal.
        raise NotImplementedError(
            "promote_scalar_leaf: register=True path is not yet implemented "
            "(OQ-003: registration stem selection is deferred)"
        )

    return MlodyValueValue(struct=promoted_struct)


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

def _tabular_value_struct(value: object) -> object | None:
    """Return the underlying value struct when *value* can be tabular."""
    if isinstance(value, MlodyValueValue):
        return value.struct
    if getattr(value, "kind", None) == "value":
        return value
    return None


def _is_explicit_tabular_value_struct(value_struct: object) -> bool:
    """Return whether *value_struct* is explicitly declared as tabular."""
    location = getattr(value_struct, "location", None)
    location_type = (
        getattr(location, "_root_kind", None)
        or getattr(location, "type", None)
        or getattr(location, "kind", None)
    )
    representation = getattr(value_struct, "representation", None)
    representation_name = (
        getattr(representation, "name", None)
        or getattr(representation, "type", None)
    )
    has_tabular_representation = representation_name in {"csv", "parquet"}
    has_tabular_source = (
        getattr(value_struct, "_source_value", None) is not None
        or getattr(value_struct, "source", None) is not None
    )

    if location_type in {"derived", "remote", "parquet"}:
        return True
    if has_tabular_representation:
        return True
    if has_tabular_source and has_tabular_representation:
        return True
    return False


def _traverse_tabular_source(
    tabular_source: object,
    value: object,
    path: tuple[object, ...],
    label: "Label",
) -> MlodyValue:
    """Traverse a tabular source uniformly across parquet/csv/derived/remote."""
    from mlody.core.sql.sql_query import MlodyQueryError, mlody_query  # noqa: PLC0415
    from mlody.core.traversal_grammar import (  # noqa: PLC0415
        FieldSegment,
        IndexSegment,
        KeySegment,
        SliceSegment,
        SqlSegment,
    )

    active_schema = None
    cached_rows: list[object] | None = None
    last_field_name: str | None = None

    def _load_rows() -> list[object]:
        nonlocal active_schema, cached_rows
        if cached_rows is None:
            preview = tabular_source.preview(tabular_source.count())
            active_schema = preview.table.schema
            cached_rows = list(preview.table.to_pylist())
        return cached_rows

    def _run_sql(query: str) -> list[dict[str, object]] | MlodyUnresolvedValue:
        nonlocal active_schema
        try:
            table = mlody_query(tabular_source.query_input(), query)
        except MlodyQueryError as exc:
            return MlodyUnresolvedValue(
                label=label,
                reason=f"SQL query failed: {exc} (label: {label!r})",
            )
        active_schema = table.schema
        return list(table.to_pylist())

    current: object = tabular_source
    for seg in path:
        if isinstance(seg, str):
            seg = FieldSegment(name=seg)

        if current is tabular_source:
            if isinstance(seg, IndexSegment):
                rows = _load_rows()
                try:
                    current = rows[seg.index]
                except IndexError as exc:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=f"tabular index error: {exc} (label: {label!r})",
                    )
            elif isinstance(seg, SliceSegment):
                rows = _load_rows()
                current = rows[slice(seg.start, seg.stop, seg.step)]
            elif isinstance(seg, SqlSegment):
                sql_rows = _run_sql(seg.query)
                if isinstance(sql_rows, MlodyUnresolvedValue):
                    return sql_rows
                current = sql_rows
            elif isinstance(seg, FieldSegment):
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"FieldSegment {seg.name!r} applied directly to tabular source "
                        f"without a preceding row index (label: {label!r})"
                    ),
                )
            else:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"unsupported path segment {type(seg).__name__!r} "
                        f"on tabular source (label: {label!r})"
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
                last_field_name = key
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
                key = seg.name if isinstance(seg, FieldSegment) else seg.key
                try:
                    current = [row[key] for row in current]  # type: ignore[index]
                except KeyError:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"column {key!r} not found in one or more rows "
                            f"(label: {label!r})"
                        ),
                    )
                except TypeError:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"cannot project field {key!r} from non-record list elements "
                            f"(label: {label!r})"
                        ),
                    )
                last_field_name = key
            elif isinstance(seg, IndexSegment):
                try:
                    current = current[seg.index]
                except IndexError as exc:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=f"index error on tabular result: {exc} (label: {label!r})",
                    )
            elif isinstance(seg, SliceSegment):
                current = current[slice(seg.start, seg.stop, seg.step)]
            else:
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"unsupported path segment {type(seg).__name__!r} "
                        f"on tabular list result (label: {label!r})"
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

    if last_field_name is not None and active_schema is not None:
        import pyarrow as _pa  # noqa: PLC0415

        try:
            arrow_field = active_schema.field(last_field_name)
            arrow_type = arrow_field.type
        except Exception:
            arrow_field = None
            arrow_type = None

        if arrow_type is not None:
            is_nested = (
                _pa.types.is_struct(arrow_type)
                or _pa.types.is_list(arrow_type)
                or _pa.types.is_map(arrow_type)
                or _pa.types.is_large_list(arrow_type)
            )
            if not is_nested:
                type_map = _get_arrow_type_map()
                mlody_type_name = type_map.get(arrow_type)
                if mlody_type_name is None:
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"field {last_field_name!r}: no mlody primitive maps to "
                            f"Arrow type {arrow_type!s} (label: {label!r})"
                        ),
                    )

                declared_type_name: str | None = None
                value_type = getattr(value, "type", None)
                direct_fields = getattr(value_type, "fields", None)
                attrs_dict = getattr(value_type, "attributes", None)
                attrs_fields = (
                    attrs_dict.get("fields")
                    if isinstance(attrs_dict, dict)
                    else None
                )
                fields_list: list[object] = list(direct_fields or attrs_fields or [])
                for field in fields_list:
                    if getattr(field, "name", None) == last_field_name:
                        field_type = getattr(field, "type", None)
                        declared_type_name = getattr(field_type, "name", None) or getattr(
                            field_type, "type", None
                        )
                        break

                if (
                    declared_type_name is not None
                    and declared_type_name != mlody_type_name
                ):
                    return MlodyUnresolvedValue(
                        label=label,
                        reason=(
                            f"field {last_field_name!r} type mismatch: "
                            f"Arrow inferred {mlody_type_name!r} but mlody declares "
                            f"{declared_type_name!r} (label: {label!r})"
                        ),
                    )

                element_type = _get_mlody_primitive_type(mlody_type_name)
                promoted = promote_scalar_leaf(
                    current,
                    last_field_name,
                    element_type,
                    label,
                )
                if promoted is not None:
                    return promoted

    return _RawAttrValue(value=current, label=label)


def _maybe_traverse_tabular_value(
    value: object,
    path: tuple[object, ...],
    label: "Label",
) -> MlodyValue | None:
    """Return a shared tabular traversal result when *value* is tabular-backed."""
    from mlody.core.traversal_grammar import SqlSegment  # noqa: PLC0415
    from mlody.core.tabular.location_specs import source_from_value  # noqa: PLC0415

    if not path:
        return None

    value_struct = _tabular_value_struct(value)
    if value_struct is None:
        if isinstance(path[0], SqlSegment):
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    "SQL/tabular traversal requires a value(...) target; "
                    f"got {type(value).__name__!r} (label: {label!r})"
                ),
            )
        return None

    if not _is_explicit_tabular_value_struct(value_struct):
        if isinstance(path[0], SqlSegment):
            value_name = str(getattr(value_struct, "name", "<unknown>"))
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    "SQL/tabular traversal requires a tabular value; "
                    f"{value_name!r} is not tabular in v1 (label: {label!r})"
                ),
            )
        return None

    try:
        tabular_source = source_from_value(value_struct)
    except ValueError as exc:
        return MlodyUnresolvedValue(
            label=label,
            reason=f"Failed to prepare tabular value for traversal: {exc} (label: {label!r})",
        )

    if tabular_source is None:
        if isinstance(path[0], SqlSegment):
            value_name = str(getattr(value_struct, "name", "<unknown>"))
            return MlodyUnresolvedValue(
                label=label,
                reason=(
                    "SQL/tabular traversal requires a tabular value; "
                    f"{value_name!r} is not tabular in v1 (label: {label!r})"
                ),
            )
        return None

    return _traverse_tabular_source(tabular_source, value_struct, path, label)


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

    existing = [
        os.path.expanduser(p) for p in paths if os.path.isfile(os.path.expanduser(p))
    ]
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

    # FR-007: Promote scalar leaf values from JSON-backed traversal (spec §6.3).
    # Check bool before int because Python bool is a subclass of int.
    if isinstance(current, (bool, int, float, str)):
        _field_name = path[-1] if path else "value"
        for _py_type, _mlody_name in _PYTHON_TYPE_TO_MLODY_NAME:
            if isinstance(current, _py_type):
                _element_type = _get_mlody_primitive_type(_mlody_name)
                _promoted = promote_scalar_leaf(
                    current, _field_name, _element_type, label
                )
                if _promoted is not None:
                    return _promoted
                break

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

    if isinstance(current, MlodyValueValue) and isinstance(
        current.struct, (list, tuple)
    ):
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

    if isinstance(current, MlodyValueValue) and isinstance(
        current.struct, (list, tuple)
    ):
        sliced_struct = current.struct[sl]
        return MlodyVectorValue(
            elements=tuple(MlodyValueValue(struct=v) for v in sliced_struct)
        )

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
        children: list[MlodyValue] = [
            _wrap_raw(v, label) for v in current.value.values()
        ]
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
    from mlody.core.virtual_value import (  # noqa: PLC0415
        lookup_runtime_attribute,
        synthesize_runtime_child,
    )

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
        return _engine_index_step(
            MlodyValueValue(struct=current_struct), field_name, policy, label
        )
    elif isinstance(field_name, KeySegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_key_step(current_struct, field_name, policy, label)
        return _engine_key_step(
            _RawAttrValue(value=current_struct, label=label), field_name, policy, label
        )
    elif isinstance(field_name, WildcardSegment):
        # Wildcard: expand current struct as a record-typed Struct value
        if isinstance(current_struct, MlodyValue):
            return _engine_wildcard_step(current_struct, field_name, policy, label)
        # Wrap as MlodyValueValue so engine handles it
        return _engine_wildcard_step(
            MlodyValueValue(struct=current_struct), field_name, policy, label
        )
    elif isinstance(field_name, RecursiveDescentSegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_recursive_descent_step(
                current_struct, field_name, policy, label
            )
        return _engine_recursive_descent_step(
            MlodyValueValue(struct=current_struct), field_name, policy, label
        )
    elif isinstance(field_name, SliceSegment):
        if isinstance(current_struct, MlodyValue):
            return _engine_slice_step(current_struct, field_name, policy, label)
        return _engine_slice_step(
            MlodyValueValue(struct=current_struct), field_name, policy, label
        )
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
    # 1. Search semantic record fields on value.type.
    # 2. Search declared framework metadata via _entity_type.
    # 3. Fall back to raw runtime fields for backward compatibility.
    # 4. Fall back to getattr(value.type, effective_name).
    # 5. If all miss, return MlodyUnresolvedValue.
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
        entity_field = lookup_runtime_attribute(current_struct, effective_name)
        if entity_field is not None:
            raw_entity_value = getattr(current_struct, effective_name, _SENTINEL)
            if raw_entity_value is not _SENTINEL:
                return _RawAttrValue(value=raw_entity_value, label=label)
            synthesized_entity_value = synthesize_runtime_child(
                current_struct,
                effective_name,
            )
            if synthesized_entity_value is not None:
                return MlodyValueValue(struct=synthesized_entity_value)
        if not (
            getattr(current_struct, "kind", None) == "value"
            and (
                getattr(value_type, "_root_kind", None) == "record"
                or getattr(value_type, "kind", None) == "record"
            )
        ):
            raw_value = getattr(current_struct, effective_name, _SENTINEL)
            if raw_value is not _SENTINEL:
                return _RawAttrValue(value=raw_value, label=label)
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
        from mlody.core.virtual_value import (  # noqa: PLC0415
            lookup_runtime_attribute,
            synthesize_runtime_child,
            traverse_virtual_value,
        )
        from mlody.core.traversal_grammar import PathSegment, FieldSegment  # noqa: PLC0415

        if not path:
            return MlodyValueValue(struct=value)

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

        if has_engine_segs:
            tabular_result = _maybe_traverse_tabular_value(value, path, label)
            if tabular_result is not None:
                return tabular_result

        if has_engine_segs or isinstance(value, MlodyVectorValue):
            # Engine-aware loop: handles IndexSegment, KeySegment,
            # WildcardSegment, RecursiveDescentSegment, and mapped traversal
            # over vector accumulators (tasks 4.2–4.3).
            return self._traverse_with_engine(
                value, path, label, traversal_error_policy
            )

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
                    reason=(f"attribute '{missing}' not found (label: {label!r})"),
                )
            return MlodyValueValue(struct=child_value)

        _vt_attrs = getattr(value_type, "attributes", None)
        _vt_attrs_fields = (
            _vt_attrs.get("fields") if isinstance(_vt_attrs, dict) else None
        )
        is_record = (
            getattr(value_type, "kind", None) == "record"
            or getattr(value_type, "_root_kind", None) == "record"
            or bool(getattr(value_type, "fields", None) or _vt_attrs_fields)
        )

        if len(str_path) == 1 and is_record:
            result = _traverse_one_step(
                value, str_path[0], (), label, traversal_error_policy
            )
            if isinstance(result, MlodyUnresolvedValue):
                return result
            if isinstance(result, MlodyValue):
                return result
            return MlodyValueValue(struct=result[0])

        if len(str_path) >= 2 and is_record:
            current: object = value
            for i, segment in enumerate(str_path):
                step = _traverse_one_step(
                    current, segment, tuple(str_path[:i]), label, traversal_error_policy
                )
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
                            tuple(str_path[i + 1 :]),
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
        _vt_parent_obj: object = value  # tracks the object before the last getattr step
        _vt_sentinel = object()
        for i, segment in enumerate(str_path):
            _vt_parent_obj = obj
            try:
                obj = getattr(obj, segment)
            except AttributeError:
                field_decl = lookup_runtime_attribute(obj, segment)
                if field_decl is not None:
                    raw_value = getattr(obj, segment, _vt_sentinel)
                    if raw_value is not _vt_sentinel:
                        obj = raw_value
                        continue
                    synthesized = synthesize_runtime_child(obj, segment)
                    if synthesized is not None:
                        obj = synthesized
                        continue
                traversed = ".".join(str_path[:i])
                parent = f" on '{traversed}'" if traversed else ""
                return MlodyUnresolvedValue(
                    label=label,
                    reason=(
                        f"attribute '{segment}' not found{parent} (label: {label!r})"
                    ),
                )
        terminal_kind = getattr(obj, "kind", None)
        if isinstance(terminal_kind, str) and terminal_kind in TRAVERSAL_STRATEGIES:
            return _wrap_struct(terminal_kind, obj)

        # FR-008 / spec §6.4: attempt scalar promotion using parent's type.fields
        # declaration — same logic as StructTraversalStrategy (no promotion without
        # a declared mlody field type for the terminal segment).
        if isinstance(obj, (bool, int, float, str)):
            _vt_terminal_segment = str_path[-1]
            _vt_parent_type = getattr(_vt_parent_obj, "type", None)
            _vt_direct_fields = getattr(_vt_parent_type, "fields", None)
            _vt_attrs_dict = getattr(_vt_parent_type, "attributes", None)
            _vt_attrs_fields = (
                _vt_attrs_dict.get("fields") if isinstance(_vt_attrs_dict, dict) else None
            )
            _vt_fields_list: list[object] = list(
                _vt_direct_fields or _vt_attrs_fields or []
            )
            _vt_field_decl: object = None
            for _vf in _vt_fields_list:
                if getattr(_vf, "name", None) == _vt_terminal_segment:
                    _vt_field_decl = _vf
                    break
            if _vt_field_decl is not None:
                _vt_declared_type = getattr(_vt_field_decl, "type", None)
                if _vt_declared_type is not None:
                    _vt_promoted = promote_scalar_leaf(
                        obj, _vt_terminal_segment, _vt_declared_type, label
                    )
                    if _vt_promoted is not None:
                        return _vt_promoted

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

        for i, seg in enumerate(path):
            if isinstance(accumulator, MlodyUnresolvedValue):
                # Short-circuit on failure
                return accumulator

            tabular_result = _maybe_traverse_tabular_value(
                accumulator, path[i:], label
            )
            if tabular_result is not None:
                return tabular_result

            # Mapped traversal applies FieldSegment and KeySegment over each element of
            # a vector accumulator.  IndexSegment is intentionally excluded: [n] on a
            # MlodyVectorValue means "index into this vector", which is handled by
            # _engine_index_step in the else branch, not by element-wise mapping.
            is_mapping_seg = isinstance(seg, (FieldSegment, KeySegment))
            is_expansion_seg = isinstance(
                seg, (WildcardSegment, RecursiveDescentSegment)
            )

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
        _loc_root_kind = getattr(location, "_root_kind", None) or getattr(
            location, "type", None
        )

        # Derived locations must be materialised before we can read rows from them.
        if _loc_root_kind == "derived":
            try:
                from mlody.core.tabular import DerivedSource, source_from_value  # noqa: PLC0415

                derived_source = source_from_value(value)
                if not isinstance(derived_source, DerivedSource):
                    raise ValueError(f"Invalid derived location: {location!r}")
                path_val: object = str(derived_source.materialize())
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
            file_paths: list[str] = [
                os.path.expanduser(str(p)) for p in path_val if str(p)
            ]
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

        # Capture the Arrow schema once from the first file for type inference
        # during FieldSegment promotion (NFR-004: no extra file I/O — schema
        # is read from an already-resolvable file, not re-opened during traversal).
        _schema: "pa.Schema | None" = None
        try:
            import pyarrow.parquet as _pq  # noqa: PLC0415

            _schema = _pq.read_schema(file_paths[0])
        except Exception:
            # Non-fatal: schema unavailable means promotion falls back to _RawAttrValue
            pass

        # Apply each path segment left-to-right, feeding each step's output
        # as the input to the next step (chained traversal, D-4).
        # current starts as a list[str] of file paths; becomes a dict (row) after
        # an IndexSegment, or a list[dict] after a SliceSegment.
        #
        # _last_field_name tracks the most recent FieldSegment/KeySegment name
        # so the terminal return site can attempt scalar promotion (spec §6.1–§6.2).
        current: object = file_paths
        _last_field_name: str | None = None
        for seg in path:
            # Defensive normalisation: honour the docstring contract that path is
            # tuple[PathSegment, ...].  Any caller that still passes a bare str
            # is wrapped here as FieldSegment so the shape-driven dispatch below
            # can do typed isinstance checks consistently.
            if isinstance(seg, str):
                seg = FieldSegment(name=seg)
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
                        from mlody.core.sql.sql_query import MlodyQueryError, mlody_query  # noqa: PLC0415

                        try:
                            table = mlody_query(paths=current, query=seg.query)
                        except MlodyQueryError as exc:
                            return MlodyUnresolvedValue(
                                label=label,
                                reason=f"SQL query failed: {exc} (label: {label!r})",
                            )
                        # Convert to list[dict] so subsequent FieldSegment steps
                        # can extract columns via the list-of-rows dispatch arm.
                        current = table.to_pylist()
                        continue
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
                    _last_field_name = key
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
                    _last_field_name = _key
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

        # Wrap terminal result in _RawAttrValue, or attempt scalar promotion
        # (FR-001, spec §6.1–§6.2) when the terminal came from a FieldSegment.
        #
        # Promotion fires only when:
        #   - a FieldSegment was the most recent consuming step (_last_field_name set)
        #   - the Arrow schema was captured successfully (_schema is not None)
        #   - the Arrow field type maps to a known mlody primitive (FR-002)
        #   - current is a scalar or list of scalars (promote_scalar_leaf non-None)
        if _last_field_name is not None and _schema is not None:
            import pyarrow as _pa  # noqa: PLC0415

            try:
                _arrow_field = _schema.field(_last_field_name)
                _arrow_type = _arrow_field.type
            except Exception:
                _arrow_field = None
                _arrow_type = None

            if _arrow_type is not None:
                # Nested Arrow types (struct, list_, map_) are never promoted —
                # return _RawAttrValue unchanged (FR-006, FR-010).
                _is_nested = (
                    _pa.types.is_struct(_arrow_type)
                    or _pa.types.is_list(_arrow_type)
                    or _pa.types.is_map(_arrow_type)
                    or _pa.types.is_large_list(_arrow_type)
                )
                if not _is_nested:
                    _type_map = _get_arrow_type_map()
                    _mlody_type_name = _type_map.get(_arrow_type)
                    if _mlody_type_name is None:
                        # Arrow type not in mapping — return MlodyUnresolvedValue (FR-002)
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"field {_last_field_name!r}: no mlody primitive maps to "
                                f"Arrow type {_arrow_type!s} (label: {label!r})"
                            ),
                        )

                    # FR-003: Validate inferred type against declared mlody field type.
                    # Walk value.type.fields to find a field with matching name.
                    _declared_type_name: str | None = None
                    _value_type = getattr(value, "type", None)
                    _direct_fields = getattr(_value_type, "fields", None)
                    _attrs_dict = getattr(_value_type, "attributes", None)
                    _attrs_fields = (
                        _attrs_dict.get("fields")
                        if isinstance(_attrs_dict, dict)
                        else None
                    )
                    _fields_list: list[object] = list(
                        _direct_fields or _attrs_fields or []
                    )
                    for _f in _fields_list:
                        if getattr(_f, "name", None) == _last_field_name:
                            _ft = getattr(_f, "type", None)
                            _declared_type_name = getattr(_ft, "name", None) or getattr(
                                _ft, "type", None
                            )
                            break

                    if (
                        _declared_type_name is not None
                        and _declared_type_name != _mlody_type_name
                    ):
                        return MlodyUnresolvedValue(
                            label=label,
                            reason=(
                                f"field {_last_field_name!r} type mismatch: "
                                f"Arrow inferred {_mlody_type_name!r} but mlody declares "
                                f"{_declared_type_name!r} (label: {label!r})"
                            ),
                        )

                    _element_type = _get_mlody_primitive_type(_mlody_type_name)
                    _promoted = promote_scalar_leaf(
                        current, _last_field_name, _element_type, label
                    )
                    if _promoted is not None:
                        return _promoted

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
            return ValueTraversalStrategy().traverse(
                root_value, label.attribute_path, label
            )
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
    if (
        label.entity is not None
        and label.entity.path is None
        and label.entity.name is None
    ):
        root_abs = (
            workspace._monorepo_root / root_path
            if root_path
            else workspace._monorepo_root
        )  # noqa: SLF001
        children = sorted(os.listdir(root_abs))
        return MlodyFolderValue(path=root_path, children=children)

    # Build the absolute path:
    # - named root (@foo//...): monorepo_root / root_path / entity_path
    # - rootless (//...): workspace_root / entity_path (workspace_root == monorepo_root
    #   when --workspace is not set, so behaviour is unchanged in that case)
    if root_path:
        abs_path = workspace._monorepo_root / root_path  # noqa: SLF001
    else:
        abs_path = workspace._workspace_root  # noqa: SLF001
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
            return MlodySourceValue(path=entity_path, abs_path=mlody_path)

        # -----------------------------------------------------------------------
        # Step 3: entity lookup
        # -----------------------------------------------------------------------
        # Derive stem: root_path / entity_path (mirrors evaluator._register logic)
        # For rootless // labels, prepend the workspace-relative path so the stem
        # matches what the evaluator registers (which is always monorepo-relative).
        stem_parts: list[str] = []
        if root_path:
            stem_parts.append(root_path)
        elif workspace._workspace_root != workspace._monorepo_root:  # noqa: SLF001
            workspace_rel = str(
                workspace._workspace_root.relative_to(workspace._monorepo_root)  # noqa: SLF001
            )
            stem_parts.append(workspace_rel)
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
        raw_path: tuple[str, ...] = entity_field_path + attr_path_tuple

        # Normalise raw_path to a homogeneous tuple[PathSegment, ...] so every
        # downstream strategy can rely on a single canonical segment type:
        #   - already-typed PathSegment instances pass through
        #   - strings with inline brackets are expanded via the traversal parser
        #     (e.g. "valid[1:4]" → FieldSegment("valid"), SliceSegment(1, 4))
        #   - plain strings are wrapped as FieldSegment(name=s)
        # After this block, downstream strategies may rely on resolved_path being
        # tuple[PathSegment, ...].
        from mlody.core.traversal_grammar import (  # noqa: PLC0415
            FieldSegment as _FS,
            PathSegment as _PS,
        )
        from mlody.core.traversal_parser import (  # noqa: PLC0415
            TraversalParseError as _TPE,
            parse_traversal_expression as _pte,
        )

        _expanded: list[_PS] = []
        for _s in raw_path:
            if isinstance(_s, _PS):
                _expanded.append(_s)
            elif isinstance(_s, str) and "[" in _s:
                try:
                    _expanded.extend(_pte(f".{_s}").segments)
                except _TPE:
                    _expanded.append(_FS(name=_s))
            elif isinstance(_s, str):
                _expanded.append(_FS(name=_s))
            else:
                # Defensive: unknown non-string, non-PathSegment type.
                _expanded.append(_s)  # type: ignore[arg-type]
        resolved_path: tuple[_PS, ...] = tuple(_expanded)
        location_of_struct = getattr(struct, "location", None)
        if getattr(location_of_struct, "type", None) == "parquet":
            result: MlodyValue = strategy.traverse(  # type: ignore[call-arg]
                struct,
                resolved_path,
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

        # Lift mlody-source-range _RawAttrValue → typed MlodySourceRangeValue so
        # the renderer has both the abs path and the workspace root.
        if isinstance(result, _RawAttrValue) and getattr(result.value, "kind", None) == "mlody-source-range":
            sr = result.value
            filepath = str(getattr(sr, "filepath", ""))
            return MlodySourceRangeValue(
                filepath=filepath,
                abs_path=workspace._monorepo_root / filepath,  # noqa: SLF001
                start_line=int(getattr(sr, "start_line", 0)),
                end_line=int(getattr(sr, "end_line", 0)),
            )

        # Apply entity_query (e.g. [1], ["key"], [*]) as a post-step after the
        # field-path traversal.  The label parser strips brackets and stores the
        # inner content, so we reconstruct "[query]" for the traversal parser.
        if label.entity_query is not None and not isinstance(
            result, MlodyUnresolvedValue
        ):
            from mlody.core.traversal_parser import (  # noqa: PLC0415
                TraversalParseError,
                parse_traversal_expression,
            )
            from mlody.core.traversal_grammar import SqlSegment  # noqa: PLC0415

            try:
                eq_expr = parse_traversal_expression(f"[{label.entity_query}]")
            except TraversalParseError:
                eq_expr = None
            if eq_expr is not None and eq_expr.segments:
                tabular_result = _maybe_traverse_tabular_value(
                    result,
                    eq_expr.segments,
                    label,
                )
                if tabular_result is not None:
                    return tabular_result
                seg = eq_expr.segments[0]
                step = _traverse_one_step(
                    result, seg, resolved_path, label, traversal_error_policy
                )
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
            f"under '{workspace._workspace_root}' (label: {label!r})"  # noqa: SLF001
        ),
    )


# ---------------------------------------------------------------------------
# Workspace traversal hook
# ---------------------------------------------------------------------------
# Registers _workspace_traverse_record with workspace.py so that
# mlody.core.workspace never needs to import from mlody.resolver.  This runs
# once at module-import time; resolver_lib already depends on core_lib so
# workspace.py is guaranteed to be importable here without a cycle.


def _workspace_traverse_record(
    obj: object,
    field_parts: list[object],
    entity_query: str | None,
    lbl: object,
) -> object:
    """Traverse a record-typed resolver value along *field_parts*.

    Extracted from workspace.py to break the core ↔ resolver BUILD dep cycle.
    """
    from mlody.core.traversal_parser import (
        TraversalParseError,
        parse_traversal_expression,
    )

    current: object = obj
    for fp_i, fp_seg in enumerate(field_parts):
        step_result = _traverse_one_step(
            current, fp_seg, tuple(field_parts[:fp_i]), lbl
        )
        if isinstance(step_result, MlodyUnresolvedValue):
            return step_result
        if isinstance(step_result, tuple):
            current = step_result[0]
        else:
            current = step_result
            break

    if entity_query is not None:
        try:
            expr = parse_traversal_expression(f"[{entity_query}]")
        except TraversalParseError:
            expr = None
        if expr is not None and expr.segments:
            tabular_result = _maybe_traverse_tabular_value(current, expr.segments, lbl)
            if tabular_result is not None:
                if isinstance(tabular_result, MlodyUnresolvedValue):
                    return tabular_result
                return getattr(tabular_result, "value", tabular_result)
            seg = expr.segments[0]
            q_result = _traverse_one_step(
                current, seg, field_parts, lbl, TraversalErrorPolicy.RAISE
            )
            if isinstance(q_result, MlodyUnresolvedValue):
                return q_result
            if isinstance(q_result, tuple):
                current = q_result[0]
            else:
                return getattr(q_result, "value", q_result)

    if isinstance(current, _RawAttrValue):
        return current.value
    return current


def _register_workspace_hook() -> None:
    from mlody.core.workspace import _register_resolver_traverse

    _register_resolver_traverse(_workspace_traverse_record)


_register_workspace_hook()
