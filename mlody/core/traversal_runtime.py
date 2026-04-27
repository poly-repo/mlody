"""Shared runtime traversal helpers for Structs, sequences, dicts, and virtual values."""

from __future__ import annotations

from typing import Protocol

from mlody.common.struct import Struct
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


class _VirtualValueAdapter:
    """Traversal behavior for declared virtual children on typed value Structs."""

    def step_named_child(self, value: object, name: str) -> object:
        assert isinstance(value, Struct)
        if name in value.as_mapping():
            return value.as_mapping()[name]
        _, step_virtual_value, _ = _virtual_value_helpers()
        return step_virtual_value(value, name)

    def step_segment(self, value: object, segment: object) -> object:
        assert isinstance(value, Struct)
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        _, step_virtual_value, _ = _virtual_value_helpers()
        return step_virtual_value(value, segment.name)

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert isinstance(value, Struct)
        _, _, iter_virtual_children = _virtual_value_helpers()
        return tuple(
            (FieldSegment(name), child) for name, child in iter_virtual_children(value)
        )

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert isinstance(value, Struct)
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return value.updated(**{segment.name: new_child})

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, Struct)
        if name in value.as_mapping():
            return True
        _, _, iter_virtual_children = _virtual_value_helpers()
        return any(child_name == name for child_name, _ in iter_virtual_children(value))


class _StructAdapter:
    """Traversal behavior for concrete Struct fields."""

    def step_named_child(self, value: object, name: str) -> object:
        assert isinstance(value, Struct)
        mapping = value.as_mapping()
        if name not in mapping:
            raise AttributeError(name)
        return mapping[name]

    def step_segment(self, value: object, segment: object) -> object:
        assert isinstance(value, Struct)
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return self.step_named_child(value, segment.name)

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert isinstance(value, Struct)
        return tuple(
            (FieldSegment(name), child) for name, child in value.as_mapping().items()
        )

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert isinstance(value, Struct)
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return value.updated(**{segment.name: new_child})

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, Struct)
        return name in value.as_mapping()


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
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, (list, tuple))
        return any(getattr(item, "name", None) == name for item in value)


class _DictAdapter:
    """Traversal behavior for dict values with string keys."""

    def step_named_child(self, value: object, name: str) -> object:
        raise AttributeError(name)

    def step_segment(self, value: object, segment: object) -> object:
        assert isinstance(value, dict)
        if not isinstance(segment, KeySegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return value[segment.key]

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        assert isinstance(value, dict)
        return tuple(
            (KeySegment(key), child) for key, child in value.items() if isinstance(key, str)
        )

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        assert isinstance(value, dict)
        if not isinstance(segment, KeySegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        updated = dict(value)
        updated[segment.key] = new_child
        return updated

    def has_named_child(self, value: object, name: str) -> bool:
        assert isinstance(value, dict)
        return name in value


class _ObjectAdapter:
    """Fallback traversal behavior for plain Python objects."""

    def step_named_child(self, value: object, name: str) -> object:
        return getattr(value, name)

    def step_segment(self, value: object, segment: object) -> object:
        if not isinstance(segment, FieldSegment):
            raise NotImplementedError(
                f"selector segment {type(segment).__name__} is not supported yet"
            )
        return getattr(value, segment.name)

    def iter_children(self, value: object) -> tuple[tuple[object, object], ...]:
        _ = value
        return ()

    def replace_child(self, value: object, segment: object, new_child: object) -> object:
        _ = (value, new_child)
        raise NotImplementedError(
            f"selector segment {type(segment).__name__} is not supported yet"
        )

    def has_named_child(self, value: object, name: str) -> bool:
        return hasattr(value, name)


_VIRTUAL_VALUE_ADAPTER = _VirtualValueAdapter()
_STRUCT_ADAPTER = _StructAdapter()
_SEQUENCE_ADAPTER = _SequenceAdapter()
_DICT_ADAPTER = _DictAdapter()
_OBJECT_ADAPTER = _ObjectAdapter()


def step_named_child(value: object, name: str) -> object:
    """Traverse a named child using the current runtime semantics."""
    if _is_virtual_value(value):
        return _VIRTUAL_VALUE_ADAPTER.step_named_child(value, name)
    if isinstance(value, Struct):
        return _STRUCT_ADAPTER.step_named_child(value, name)
    if isinstance(value, (list, tuple)):
        return _SEQUENCE_ADAPTER.step_named_child(value, name)
    return _OBJECT_ADAPTER.step_named_child(value, name)


def step_segment(value: object, segment: object) -> object:
    """Traverse one selector segment on a runtime value."""
    if isinstance(segment, FieldSegment):
        if _is_virtual_value(value):
            return _VIRTUAL_VALUE_ADAPTER.step_segment(value, segment)
        if isinstance(value, Struct):
            return _STRUCT_ADAPTER.step_segment(value, segment)
        return _OBJECT_ADAPTER.step_segment(value, segment)

    if isinstance(segment, IndexSegment):
        if not isinstance(value, (list, tuple)):
            raise TypeError(
                f"IndexSegment requires a sequence, got {type(value).__name__}"
            )
        return _SEQUENCE_ADAPTER.step_segment(value, segment)

    if isinstance(segment, KeySegment):
        if not isinstance(value, dict):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        return _DICT_ADAPTER.step_segment(value, segment)

    if isinstance(segment, SliceSegment):
        if not isinstance(value, (list, tuple)):
            raise TypeError(
                f"SliceSegment requires a sequence, got {type(value).__name__}"
            )
        return _SEQUENCE_ADAPTER.step_segment(value, segment)

    raise NotImplementedError(
        f"selector segment {type(segment).__name__} is not supported yet"
    )


def iter_children(value: object) -> tuple[tuple[object, object], ...]:
    """Return the immediate traversable children of *value*."""
    if _is_virtual_value(value):
        return _VIRTUAL_VALUE_ADAPTER.iter_children(value)
    if isinstance(value, Struct):
        return _STRUCT_ADAPTER.iter_children(value)
    if isinstance(value, (list, tuple)):
        return _SEQUENCE_ADAPTER.iter_children(value)
    if isinstance(value, dict):
        return _DICT_ADAPTER.iter_children(value)
    return _OBJECT_ADAPTER.iter_children(value)


def replace_child(value: object, segment: object, new_child: object) -> object:
    """Return *value* with *segment* replaced by *new_child*."""
    if isinstance(segment, FieldSegment):
        if not isinstance(value, Struct):
            raise TypeError(
                f"FieldSegment requires a Struct, got {type(value).__name__}"
            )
        if _is_virtual_value(value):
            return _VIRTUAL_VALUE_ADAPTER.replace_child(value, segment, new_child)
        return _STRUCT_ADAPTER.replace_child(value, segment, new_child)

    if isinstance(segment, IndexSegment):
        if not isinstance(value, list):
            raise TypeError(f"IndexSegment requires a list, got {type(value).__name__}")
        return _SEQUENCE_ADAPTER.replace_child(value, segment, new_child)

    if isinstance(segment, KeySegment):
        if not isinstance(value, dict):
            raise TypeError(f"KeySegment requires a dict, got {type(value).__name__}")
        return _DICT_ADAPTER.replace_child(value, segment, new_child)

    if isinstance(segment, SliceSegment):
        if not isinstance(value, list):
            raise TypeError(f"SliceSegment requires a list, got {type(value).__name__}")
        return _SEQUENCE_ADAPTER.replace_child(value, segment, new_child)

    raise NotImplementedError(
        f"selector segment {type(segment).__name__} is not supported yet"
    )


def has_named_child(value: object, name: str) -> bool:
    """Return True when *value* exposes *name* as a traversable child."""
    if _is_virtual_value(value):
        return _VIRTUAL_VALUE_ADAPTER.has_named_child(value, name)
    if isinstance(value, Struct):
        return _STRUCT_ADAPTER.has_named_child(value, name)
    if isinstance(value, (list, tuple)):
        return _SEQUENCE_ADAPTER.has_named_child(value, name)
    if isinstance(value, dict):
        return _DICT_ADAPTER.has_named_child(value, name)
    return _OBJECT_ADAPTER.has_named_child(value, name)
