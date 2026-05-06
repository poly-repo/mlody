"""Concrete setter strategies for mlody selector-based assignment."""

from __future__ import annotations

from mlody.common.struct import is_struct_like

from mlody.core.place import AssignmentMode, MISSING_PLACE_VALUE, Place
from mlody.core.traversal_runtime import replace_child, step_segment
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
        if not (is_struct_like(place.owner) or isinstance(place.owner, dict)):
            raise TypeError(
                f"expected Struct-like or dict owner, got {type(place.owner).__name__}"
            )
        if not isinstance(segment, FieldSegment):
            raise TypeError(f"expected FieldSegment, got {type(segment).__name__}")
        try:
            _ = step_segment(place.owner, segment)
        except AttributeError:
            if place.current_value is MISSING_PLACE_VALUE or place.missing:
                return
            raise

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, FieldSegment)
        return replace_child(place.owner, segment, new_value)


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
        _ = step_segment(place.owner, segment)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, IndexSegment)
        return replace_child(place.owner, segment, new_value)


class DictKeySetter:
    """Placeholder strategy for direct key writes on dict-like values."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, dict):
            raise TypeError(f"expected dict owner, got {type(place.owner).__name__}")
        if not isinstance(segment, (FieldSegment, KeySegment)):
            raise TypeError(
                f"expected FieldSegment or KeySegment, got {type(segment).__name__}"
            )
        _ = step_segment(place.owner, segment)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, (FieldSegment, KeySegment))
        return replace_child(place.owner, segment, new_value)


class SequenceSliceSetter:
    """Placeholder strategy for projected writes over sequence slices."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(place.owner, list):
            raise TypeError(f"expected list owner, got {type(place.owner).__name__}")
        if not isinstance(segment, SliceSegment):
            raise TypeError(f"expected SliceSegment, got {type(segment).__name__}")
        _ = step_segment(place.owner, segment)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = mode
        self.preflight(place, new_value, mode="copy")
        segment = _terminal_segment(place)
        assert isinstance(segment, SliceSegment)
        return replace_child(place.owner, segment, new_value)
