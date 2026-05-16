"""Concrete setter strategies for mlody selector-based assignment."""

from __future__ import annotations

from abc import ABC, abstractmethod

from mlody.common.struct import is_struct_like

from mlody.core.place import AssignmentMode, MISSING_PLACE_VALUE, Place
from mlody.core.traversal_runtime import replace_child, step_segment
from mlody.core.traversal_grammar import FieldSegment, IndexSegment, KeySegment, SliceSegment
from mlody.core.virtual_value import is_virtual_value


def _terminal_segment(place: Place) -> object:
    if not place.selector.segments:
        raise ValueError("place selector is empty")
    return place.selector.segments[-1]


class FieldSetter(ABC):
    """Abstract base class for all setter strategies.

    Provides shared preflight/commit machinery; subclasses override only the
    three _validate_* hook methods (or override preflight/commit entirely when
    the standard pattern does not apply).
    """

    @classmethod
    @abstractmethod
    def matches(cls, seg: object, owner: object) -> bool:
        """Return True when this strategy applies to the given segment and owner."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        """Validate the write without committing it."""
        _ = mode
        seg = _terminal_segment(place)
        self._validate_segment(seg)
        self._validate_owner(place.owner)
        self._validate_value(place, new_value, seg)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        """Apply the write and return the updated owner."""
        self.preflight(place, new_value, mode="copy")
        seg = _terminal_segment(place)
        return replace_child(place.owner, seg, new_value)

    def _validate_segment(self, seg: object) -> None:
        """Hook: raise if the segment type is not acceptable."""

    def _validate_owner(self, owner: object) -> None:
        """Hook: raise if the owner type is not acceptable."""

    def _validate_value(self, place: Place, new_value: object, seg: object) -> None:
        """Hook: raise if the new value is not compatible with the place."""


class StructFieldSetter(FieldSetter):
    """Strategy for direct field writes on struct-like values."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        return isinstance(seg, FieldSegment) and (is_struct_like(owner) or isinstance(owner, dict))

    def _validate_segment(self, seg: object) -> None:
        if not isinstance(seg, FieldSegment):
            raise TypeError(f"expected FieldSegment, got {type(seg).__name__}")

    def _validate_owner(self, owner: object) -> None:
        if not (is_struct_like(owner) or isinstance(owner, dict)):
            raise TypeError(
                f"expected Struct-like or dict owner, got {type(owner).__name__}"
            )

    def _validate_value(self, place: Place, new_value: object, seg: object) -> None:
        _ = new_value
        assert isinstance(seg, FieldSegment)
        try:
            _ = step_segment(place.owner, seg)
        except AttributeError:
            if place.current_value is MISSING_PLACE_VALUE or place.missing:
                return
            raise


class VirtualValueFieldSetter(FieldSetter):
    """Explicit extension seam for virtual-value assignment targets."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        return isinstance(seg, FieldSegment) and is_virtual_value(owner)

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


class ReadOnlyFieldSetter(FieldSetter):
    """Explicit failure strategy for synthetic read-only runtime selectors."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        # Never matched via the strategy list; selected by explicit pre-check in setf.py
        # because matching requires runtime_attr which is computed in _make_direct_place.
        return False

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (new_value, mode)
        segment = _terminal_segment(place)
        if not isinstance(segment, FieldSegment):
            raise TypeError(f"expected FieldSegment, got {type(segment).__name__}")
        raise NotImplementedError(
            "assignment through read-only runtime selectors is not supported"
        )

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        self.preflight(place, new_value, mode=mode)
        raise AssertionError("read-only runtime commit should be unreachable after preflight")


class ListIndexSetter(FieldSetter):
    """Strategy for direct index writes on list-like values."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        return isinstance(seg, IndexSegment) and isinstance(owner, list)

    def _validate_segment(self, seg: object) -> None:
        if not isinstance(seg, IndexSegment):
            raise TypeError(f"expected IndexSegment, got {type(seg).__name__}")

    def _validate_owner(self, owner: object) -> None:
        if not isinstance(owner, list):
            raise TypeError(f"expected list owner, got {type(owner).__name__}")

    def _validate_value(self, place: Place, new_value: object, seg: object) -> None:
        _ = new_value
        assert isinstance(seg, IndexSegment)
        _ = step_segment(place.owner, seg)


class DictKeySetter(FieldSetter):
    """Strategy for direct key writes on dict-like values."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        return isinstance(seg, (FieldSegment, KeySegment)) and isinstance(owner, dict)

    def _validate_segment(self, seg: object) -> None:
        if not isinstance(seg, (FieldSegment, KeySegment)):
            raise TypeError(
                f"expected FieldSegment or KeySegment, got {type(seg).__name__}"
            )

    def _validate_owner(self, owner: object) -> None:
        if not isinstance(owner, dict):
            raise TypeError(f"expected dict owner, got {type(owner).__name__}")

    def _validate_value(self, place: Place, new_value: object, seg: object) -> None:
        _ = new_value
        assert isinstance(seg, (FieldSegment, KeySegment))
        _ = step_segment(place.owner, seg)


class SequenceSliceSetter(FieldSetter):
    """Strategy for projected writes over sequence slices."""

    @classmethod
    def matches(cls, seg: object, owner: object) -> bool:
        return isinstance(seg, SliceSegment) and isinstance(owner, list)

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


_STRATEGIES: list[type[FieldSetter]] = [
    VirtualValueFieldSetter,
    StructFieldSetter,
    DictKeySetter,
    ListIndexSetter,
    SequenceSliceSetter,
]
