"""Shared runtime traversal helpers for Struct-like values, sequences, dicts, and virtual values."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from mlody.common.struct import Struct, is_struct_like, struct_like_as_mapping, struct_like_updated
from mlody.core.traversal_grammar import FieldSegment, IndexSegment, KeySegment, SliceSegment


class TraversalAdapter(Protocol):
    """Small strategy surface for runtime traversal behaviors."""

    def step_named_child(self, value: object, name: str) -> object: ...

    def step_segment(self, value: object, segment: object) -> object: ...

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]: ...

    def replace_child(self, value: object, segment: object, new_child: object) -> object: ...

    def has_named_child(self, value: object, name: str) -> bool: ...


def _virtual_value_helpers() -> tuple[object, object, object]:
    from mlody.core.virtual_value import (  # noqa: PLC0415
        is_virtual_value,
        iter_virtual_children,
        step_virtual_value,
    )

    return is_virtual_value, step_virtual_value, iter_virtual_children


def _is_virtual_value(value: object) -> bool:
    is_virtual_value, _, _ = _virtual_value_helpers()
    return bool(is_virtual_value(value))


def _synthesized_runtime_child(value: object, name: str) -> object | None:
    from mlody.core.virtual_value import synthesize_runtime_child  # noqa: PLC0415

    return synthesize_runtime_child(value, name)


class _VirtualValueAdapter:
    """Traversal behavior for declared virtual children on typed value entities."""

    def step_named_child(self, value: object, name: str) -> object:
        assert is_struct_like(value)
        mapping = struct_like_as_mapping(value)
        if name in mapping:
            return mapping[name]
        _, step_virtual_value, _ = _virtual_value_helpers()
        return step_virtual_value(value, name)

    def step_segment(self, value: object, segment: object) -> object:
        assert is_struct_like(value)
        if isinstance(segment, IndexSegment):
            raise TypeError(
                f"IndexSegment requires a sequence, got {type(value).__name__}"
            )
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(
                f"SliceSegment requires a sequence, got {type(value).__name__}"
            )
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        _, step_virtual_value, _ = _virtual_value_helpers()
        return step_virtual_value(value, segment.name)

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert is_struct_like(value)
        _, _, iter_virtual_children = _virtual_value_helpers()
        return tuple(
            (FieldSegment(name), child) for name, child in iter_virtual_children(value)
        )

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert is_struct_like(value)
        if isinstance(segment, IndexSegment):
            raise TypeError(f"IndexSegment requires a list, got {type(value).__name__}")
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(f"SliceSegment requires a list, got {type(value).__name__}")
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return struct_like_updated(value, **{segment.name: new_child})

    def has_named_child(self, value: object, name: str) -> bool:
        assert is_struct_like(value)
        if name in struct_like_as_mapping(value):
            return True
        _, _, iter_virtual_children = _virtual_value_helpers()
        return any(child_name == name for child_name, _ in iter_virtual_children(value))


class _StructAdapter:
    """Traversal behavior for concrete Struct-like fields."""

    def step_named_child(self, value: object, name: str) -> object:
        assert is_struct_like(value)
        mapping = struct_like_as_mapping(value)
        if name not in mapping:
            synthesized = _synthesized_runtime_child(value, name)
            if synthesized is not None:
                return synthesized
            raise AttributeError(name)
        return mapping[name]

    def step_segment(self, value: object, segment: object) -> object:
        assert is_struct_like(value)
        if isinstance(segment, IndexSegment):
            raise TypeError(
                f"IndexSegment requires a sequence, got {type(value).__name__}"
            )
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(
                f"SliceSegment requires a sequence, got {type(value).__name__}"
            )
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return self.step_named_child(value, segment.name)

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert is_struct_like(value)
        mapping = struct_like_as_mapping(value)
        children = [(FieldSegment(name), child) for name, child in mapping.items()]
        seen = set(mapping)
        from mlody.core.virtual_value import (  # noqa: PLC0415
            iter_declared_attributes,
            iter_runtime_method_attributes,
        )

        for attr_spec in iter_declared_attributes(getattr(value, "type", None)):
            name = getattr(attr_spec, "name", None)
            if isinstance(name, str) and name not in seen:
                synthesized = _synthesized_runtime_child(value, name)
                if synthesized is not None:
                    children.append((FieldSegment(name), synthesized))
                    seen.add(name)
        entity_type = getattr(value, "_entity_type", None)
        for attr_spec in iter_declared_attributes(entity_type):
            name = getattr(attr_spec, "name", None)
            if isinstance(name, str) and name not in seen:
                synthesized = _synthesized_runtime_child(value, name)
                if synthesized is not None:
                    children.append((FieldSegment(name), synthesized))
                    seen.add(name)
        for attr_spec in iter_runtime_method_attributes(value):
            name = getattr(attr_spec, "name", None)
            if isinstance(name, str) and name not in seen:
                synthesized = _synthesized_runtime_child(value, name)
                if synthesized is not None:
                    children.append((FieldSegment(name), synthesized))
                    seen.add(name)
        return tuple(children)

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert is_struct_like(value)
        if isinstance(segment, IndexSegment):
            raise TypeError(f"IndexSegment requires a list, got {type(value).__name__}")
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(f"SliceSegment requires a list, got {type(value).__name__}")
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return struct_like_updated(value, **{segment.name: new_child})

    def has_named_child(self, value: object, name: str) -> bool:
        assert is_struct_like(value)
        return name in struct_like_as_mapping(value) or _synthesized_runtime_child(value, name) is not None


class _SequenceAdapter:
    """Traversal behavior for lists and tuples."""

    def step_named_child(self, value: object, name: str) -> object:
        assert isinstance(value, (list, tuple))
        for item in value:
            if getattr(item, "name", None) == name:
                return item
        raise KeyError(name)

    def step_segment(self, value: object, segment: object) -> object:
        assert isinstance(value, (list, tuple))
        if isinstance(segment, IndexSegment):
            return value[segment.index]
        if isinstance(segment, SliceSegment):
            return list(value[slice(segment.start, segment.stop, segment.step)])
        if isinstance(segment, FieldSegment):
            raise TypeError(
                f"FieldSegment requires a Struct-like value or dict, got {type(value).__name__}"
            )
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert isinstance(value, (list, tuple))
        return tuple((IndexSegment(index), child) for index, child in enumerate(value))

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        if not isinstance(value, list):
            raise TypeError(f"{type(segment).__name__} requires a list, got {type(value).__name__}")
        if isinstance(segment, IndexSegment):
            updated = list(value)
            updated[segment.index] = new_child
            return updated
        if isinstance(segment, SliceSegment):
            updated = list(value)
            for index in range(
                *slice(segment.start, segment.stop, segment.step).indices(len(updated))
            ):
                updated[index] = new_child
            return updated
        if isinstance(segment, FieldSegment):
            raise TypeError(
                f"FieldSegment requires a Struct-like value or dict, got {type(value).__name__}"
            )
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, (list, tuple))
        return any(getattr(item, "name", None) == name for item in value)


class _DictAdapter:
    """Traversal behavior for dict values with string keys."""

    def step_named_child(self, value: object, name: str) -> object:
        assert isinstance(value, dict)
        if name not in value:
            raise AttributeError(name)
        return value[name]

    def step_segment(self, value: object, segment: object) -> object:
        assert isinstance(value, dict)
        if isinstance(segment, FieldSegment):
            return self.step_named_child(value, segment.name)
        if isinstance(segment, KeySegment):
            return value[segment.key]
        if isinstance(segment, IndexSegment):
            raise TypeError(
                f"IndexSegment requires a sequence, got {type(value).__name__}"
            )
        if isinstance(segment, SliceSegment):
            raise TypeError(
                f"SliceSegment requires a sequence, got {type(value).__name__}"
            )
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert isinstance(value, dict)
        return tuple(
            (KeySegment(key), child) for key, child in value.items() if isinstance(key, str)
        )

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert isinstance(value, dict)
        if isinstance(segment, FieldSegment):
            updated = dict(value)
            updated[segment.name] = new_child
            return updated
        if isinstance(segment, KeySegment):
            updated = dict(value)
            updated[segment.key] = new_child
            return updated
        if isinstance(segment, IndexSegment):
            raise TypeError(f"IndexSegment requires a list, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(f"SliceSegment requires a list, got {type(value).__name__}")
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, dict)
        return name in value


class _ObjectAdapter:
    """Fallback traversal behavior for plain Python objects."""

    def step_named_child(self, value: object, name: str) -> object:
        try:
            return getattr(value, name)
        except AttributeError:
            synthesized = _synthesized_runtime_child(value, name)
            if synthesized is not None:
                return synthesized
            raise

    def step_segment(self, value: object, segment: object) -> object:
        if isinstance(segment, FieldSegment):
            return self.step_named_child(value, segment.name)
        if isinstance(segment, IndexSegment):
            raise TypeError(
                f"IndexSegment requires a sequence, got {type(value).__name__}"
            )
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(
                f"SliceSegment requires a sequence, got {type(value).__name__}"
            )
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        _ = value
        return ()

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        _ = new_child
        if isinstance(segment, FieldSegment):
            raise TypeError(
                f"FieldSegment requires a Struct-like value or dict, got {type(value).__name__}"
            )
        if isinstance(segment, IndexSegment):
            raise TypeError(f"IndexSegment requires a list, got {type(value).__name__}")
        if isinstance(segment, KeySegment):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        if isinstance(segment, SliceSegment):
            raise TypeError(f"SliceSegment requires a list, got {type(value).__name__}")
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def has_named_child(self, value: object, name: str) -> bool:
        return hasattr(value, name) or _synthesized_runtime_child(value, name) is not None


_VIRTUAL_VALUE_ADAPTER = _VirtualValueAdapter()
_STRUCT_ADAPTER = _StructAdapter()
_SEQUENCE_ADAPTER = _SequenceAdapter()
_DICT_ADAPTER = _DictAdapter()
_OBJECT_ADAPTER = _ObjectAdapter()

# Predicate-list registry for adapter selection.  Ordered by priority: virtual
# values first, then struct-like, then dict, then sequence.  The fallback
# (_OBJECT_ADAPTER) is used when no predicate matches.
_ADAPTER_RULES: list[tuple[Callable[[object], bool], TraversalAdapter]] = [
    (_is_virtual_value, _VIRTUAL_VALUE_ADAPTER),
    (is_struct_like, _STRUCT_ADAPTER),
    (lambda v: isinstance(v, dict), _DICT_ADAPTER),
    (lambda v: isinstance(v, (list, tuple)), _SEQUENCE_ADAPTER),
]


def _adapter_for(value: object) -> TraversalAdapter:
    """Return the correct TraversalAdapter for *value* by consulting _ADAPTER_RULES."""
    for predicate, adapter in _ADAPTER_RULES:
        if predicate(value):
            return adapter
    return _OBJECT_ADAPTER


def step_named_child(value: object, name: str) -> object:
    """Traverse a named child using the current runtime semantics."""
    return _adapter_for(value).step_named_child(value, name)


def step_segment(value: object, segment: object) -> object:
    """Traverse one selector segment on a runtime value."""
    return _adapter_for(value).step_segment(value, segment)


def iter_children(value: object) -> tuple[tuple[object, object], ...]:
    """Return the immediate traversable children of *value*."""
    return _adapter_for(value).iter_children(value)


def replace_child(value: object, segment: object, new_child: object) -> object:
    """Return *value* with *segment* replaced by *new_child*."""
    return _adapter_for(value).replace_child(value, segment, new_child)


def has_named_child(value: object, name: str) -> bool:
    """Return True when *value* exposes *name* as a traversable child."""
    return _adapter_for(value).has_named_child(value, name)
