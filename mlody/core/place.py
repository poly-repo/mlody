"""Writable place model for mlody selector-based assignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from mlody.core.traversal_grammar import PathExpression

AssignmentMode = Literal["inplace", "copy"]
MISSING_PLACE_VALUE = object()


class SetterStrategy(Protocol):
    """Protocol implemented by concrete setter strategies."""

    def preflight(self, place: "Place", new_value: object, *, mode: AssignmentMode) -> None:
        """Validate that the write can be applied."""

    def commit(self, place: "Place", new_value: object, *, mode: AssignmentMode) -> object:
        """Apply the write and return the updated value or owner."""


@dataclass(frozen=True)
class Place:
    """One writable target resolved from a selector."""

    root: object
    owner: object
    selector: PathExpression
    accessor: str
    current_value: object
    declared_type: object | None
    declared_representation: object | None
    strategy: SetterStrategy
    missing: bool = False
    projected: bool = False
    lineage_sink: object | None = None
    lineage_selector: PathExpression | None = None


@dataclass(frozen=True)
class PlaceSet:
    """Collection of writable places resolved from one selector."""

    places: tuple[Place, ...]

    def uniform_type(self) -> object | None:
        """Return the shared declared type, if any."""
        if not self.places:
            return None
        first = self.places[0].declared_type
        for place in self.places[1:]:
            if place.declared_type != first:
                raise ValueError("places do not share a uniform declared type")
        return first

    def uniform_representation(self) -> object | None:
        """Return the shared declared representation, if any."""
        if not self.places:
            return None
        first = self.places[0].declared_representation
        for place in self.places[1:]:
            if place.declared_representation != first:
                raise ValueError("places do not share a uniform representation")
        return first

    def assert_non_empty(self) -> None:
        """Raise when the place set is empty."""
        if not self.places:
            raise ValueError("selector resolved to no writable places")

    def assert_uniform_contract(self) -> None:
        """Raise when type or representation contracts are heterogeneous."""
        self.assert_non_empty()
        self.uniform_type()
        self.uniform_representation()
