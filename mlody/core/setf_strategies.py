"""Concrete setter strategies for mlody selector-based assignment."""

from __future__ import annotations

from common.python.starlarkish.core.struct import Struct

from mlody.core.place import AssignmentMode, Place
from mlody.core.traversal_grammar import FieldSegment, IndexSegment, KeySegment, SliceSegment
from mlody.core.virtual_value import is_virtual_value


def _terminal_segment(place: Place) -> object:
    if not place.selector.segments:
        raise ValueError("place selector is empty")
    return place.selector.segments[-1]


class StructFieldSetter:
    """Placeholder strategy for direct field writes on struct-like values."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, Struct):
            raise TypeError(f"expected Struct owner, got {type(place.owner).__name__}")
        if not isinstance(segment, FieldSegment):
            raise TypeError(f"expected FieldSegment, got {type(segment).__name__}")
        if segment.name not in place.owner.as_mapping():
            raise AttributeError(segment.name)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, FieldSegment)
        updated = dict(place.owner.as_mapping())
        updated[segment.name] = new_value
        return Struct(**updated)


class VirtualValueFieldSetter:
    """Explicit extension seam for virtual-value assignment targets."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(segment, FieldSegment):
            raise TypeError(f"expected FieldSegment, got {type(segment).__name__}")
        if not is_virtual_value(place.owner):
            raise TypeError(f"expected virtual value owner, got {type(place.owner).__name__}")
        raise NotImplementedError(
            "assignment through virtual value selectors is not supported yet"
        )

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        self.preflight(place, new_value, mode=mode)
        raise AssertionError("virtual value commit should be unreachable after preflight")


class ListIndexSetter:
    """Placeholder strategy for direct index writes on list-like values."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, list):
            raise TypeError(f"expected list owner, got {type(place.owner).__name__}")
        if not isinstance(segment, IndexSegment):
            raise TypeError(f"expected IndexSegment, got {type(segment).__name__}")
        _ = place.owner[segment.index]

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, IndexSegment)
        updated = list(place.owner)
        updated[segment.index] = new_value
        return updated


class DictKeySetter:
    """Placeholder strategy for direct key writes on dict-like values."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, dict):
            raise TypeError(f"expected dict owner, got {type(place.owner).__name__}")
        if not isinstance(segment, KeySegment):
            raise TypeError(f"expected KeySegment, got {type(segment).__name__}")
        if segment.key not in place.owner:
            raise KeyError(segment.key)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, KeySegment)
        updated = dict(place.owner)
        updated[segment.key] = new_value
        return updated


class SequenceSliceSetter:
    """Placeholder strategy for projected writes over sequence slices."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, list):
            raise TypeError(f"expected list owner, got {type(place.owner).__name__}")
        if not isinstance(segment, SliceSegment):
            raise TypeError(f"expected SliceSegment, got {type(segment).__name__}")
        _ = range(*slice(segment.start, segment.stop, segment.step).indices(len(place.owner)))

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, SliceSegment)
        updated = list(place.owner)
        for index in range(*slice(segment.start, segment.stop, segment.step).indices(len(updated))):
            updated[index] = new_value
        return updated
