"""Tests for the public place-model skeleton.

These tests cover the initial OpenSpec foundation tasks for
`mav-515-setf-python`.
"""

from __future__ import annotations

from typing import Protocol, get_args

import pytest

from mlody.core.place import AssignmentMode, Place, PlaceSet, SetterStrategy
from mlody.core.traversal_grammar import FieldSegment, PathExpression


class TestPlaceModuleSkeleton:
    """Foundation tests for the place model public surface."""

    def test_assignment_mode_exposes_inplace_and_copy_literals(self) -> None:
        """Task 1.1 / 2.1: AssignmentMode exposes the two v1 write modes."""
        assert get_args(AssignmentMode) == ("inplace", "copy")

    def test_setter_strategy_is_protocol(self) -> None:
        """Task 1.1 / 2.1: SetterStrategy is a typing Protocol."""
        assert issubclass(SetterStrategy, Protocol)

    def test_place_and_place_set_are_constructible(self) -> None:
        """Task 1.1 / 2.1: the public dataclasses can be instantiated."""
        selector = PathExpression(segments=(FieldSegment("learning_rate"),))
        strategy = _StubStrategy()
        place = Place(
            root={"config": {"learning_rate": 0.1}},
            owner={"learning_rate": 0.1},
            selector=selector,
            accessor=".learning_rate",
            current_value=0.1,
            declared_type="float",
            declared_representation="python-float",
            strategy=strategy,
        )

        place_set = PlaceSet(places=(place,))

        assert place.accessor == ".learning_rate"
        assert place_set.places == (place,)

    def test_place_set_assert_non_empty_raises_for_empty_collection(self) -> None:
        """Task 4.1: empty place sets are rejected explicitly."""
        with pytest.raises(ValueError, match="no writable places"):
            PlaceSet(places=()).assert_non_empty()

    def test_place_set_assert_uniform_contract_raises_for_mixed_types(self) -> None:
        """Task 4.2: type contracts must be uniform across a bulk assignment."""
        selector = PathExpression(segments=(FieldSegment("value"),))
        strategy = _StubStrategy()
        left = Place(
            root=object(),
            owner=object(),
            selector=selector,
            accessor=".value",
            current_value=1,
            declared_type="integer",
            declared_representation="plain",
            strategy=strategy,
        )
        right = Place(
            root=object(),
            owner=object(),
            selector=selector,
            accessor=".value",
            current_value="1",
            declared_type="string",
            declared_representation="plain",
            strategy=strategy,
        )

        with pytest.raises(ValueError, match="uniform declared type"):
            PlaceSet(places=(left, right)).assert_uniform_contract()


class _StubStrategy:
    """Minimal setter strategy used only for skeleton tests."""

    def preflight(self, place: Place, new_value: object, *, mode: AssignmentMode) -> None:
        _ = (place, new_value, mode)

    def commit(self, place: Place, new_value: object, *, mode: AssignmentMode) -> object:
        _ = (place, mode)
        return new_value
