"""RecursiveDescentStepEngine — applies a RecursiveDescentSegment during label traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlody.core.traversal_runtime import iter_children
from mlody.resolver.engine.dispatch import register_step_engine
from mlody.resolver.resolver_impl import (
    TraversalErrorPolicy,
    _collect_record_fields,
    _is_record_struct,
    _policy_miss,
    _wrap_raw,
    is_registry_backed,
)
from mlody.resolver.values.base import MlodyValue
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.structural import MlodyVectorValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


class RecursiveDescentStepEngine:
    """Apply a RecursiveDescentSegment to a MlodyValue.

    Collects all descendants at any depth using depth-first traversal.
    The current value itself is NOT included.  Recurses into:
    - ``MlodyVectorValue`` elements
    - ``_RawAttrValue`` wrapping a Python ``dict`` (values)
    - Record-typed Starlark Structs (all declared fields)

    Does not recurse into scalar leaves.
    Non-traversable roots follow the RAISE/SKIP policy.
    """

    def apply(
        self,
        value: MlodyValue,
        segment: object,
        policy: TraversalErrorPolicy,
        label: "Label",
    ) -> MlodyValue:
        collected: list[MlodyValue] = []
        _visited: set[int] = set()

        def _collect_children(node: object) -> list[MlodyValue]:
            """Return the immediate MlodyValue children of *node*.

            Accepts both typed ``MlodyValue`` wrappers and raw Starlark Structs
            so the engine can be called directly with an unwrapped struct without
            extra wrapping.
            """
            from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

            if isinstance(node, MlodyVectorValue):
                return list(node.elements)

            if isinstance(node, _RawAttrValue) and isinstance(node.value, dict):
                # Delegate child iteration to the shared adapter so dict
                # dispatch logic is not duplicated in each engine.
                return [_wrap_raw(child, label) for _, child in iter_children(node.value)]

            if is_registry_backed(node):
                struct_obj = node.struct  # type: ignore[union-attr]
                if isinstance(struct_obj, _Struct) and _is_record_struct(struct_obj):
                    return _collect_record_fields(struct_obj, label)

            # Raw Struct with record type — reached when engine is called directly
            # with an unwrapped struct (e.g. tests or mapped-traversal intermediates).
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

        # Verify the root is traversable before descending
        from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

        root_is_traversable = (
            isinstance(value, MlodyVectorValue)
            or (isinstance(value, _RawAttrValue) and isinstance(value.value, dict))
            or (
                is_registry_backed(value)
                and isinstance(getattr(value, "struct", None), _Struct)
                and _is_record_struct(getattr(value, "struct", None))
            )
            # Raw Struct with record type — reached when the engine is called directly.
            or (isinstance(value, _Struct) and _is_record_struct(value))  # type: ignore[arg-type]
        )

        if not root_is_traversable:
            return _policy_miss(
                policy,
                label,
                (
                    f"RecursiveDescentSegment cannot traverse {type(value).__name__}; "
                    "expected a vector, dict-backed value, or record-typed Struct "
                    f"(label: {label!r})"
                ),
            )

        _dfs(value)
        return MlodyVectorValue(elements=tuple(collected))


register_step_engine("RecursiveDescentSegment", RecursiveDescentStepEngine())
