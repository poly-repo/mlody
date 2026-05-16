"""Tests for FieldSetter ABC and all concrete strategy classes (F12)."""

from __future__ import annotations

import abc

import pytest

from common.python.starlarkish.core.struct import Struct

from mlody.core.place import MISSING_PLACE_VALUE, AssignmentMode, Place
from mlody.core.setf_strategies import (
    DictKeySetter,
    FieldSetter,
    ListIndexSetter,
    ReadOnlyFieldSetter,
    SequenceSliceSetter,
    StructFieldSetter,
    VirtualValueFieldSetter,
)
from mlody.core.traversal_grammar import FieldSegment, IndexSegment, KeySegment, PathExpression, SliceSegment
from mlody.core.virtual_value import make_virtual_value


def _make_place(
    owner: object,
    segment: object,
    current_value: object = None,
    *,
    missing: bool = False,
) -> Place:
    """Construct a minimal Place for testing strategy methods."""
    return Place(
        root=owner,
        owner=owner,
        selector=PathExpression(segments=(segment,)),
        accessor=str(segment),
        current_value=current_value if current_value is not None else MISSING_PLACE_VALUE,
        declared_type=None,
        declared_representation=None,
        strategy=StructFieldSetter(),
        missing=missing,
    )


_VIRTUAL_TYPE = Struct(
    kind="type",
    type="virtual_test",
    name="virtual_test",
    fields=[Struct(name="x", type=Struct(kind="type", type="string", name="string"))],
    attributes={},
    _allowed_attrs={},
)


def _make_virtual_owner() -> object:
    return make_virtual_value(
        value_type=_VIRTUAL_TYPE,
        label="'test",
        materializer=lambda _v: Struct(x="real"),
    )


class TestFieldSetterABC:
    """FieldSetter is an abstract base class — spec scenario: FieldSetter is an ABC."""

    def test_fieldsetter_is_abc(self) -> None:
        assert issubclass(FieldSetter, abc.ABC)

    def test_fieldsetter_cannot_be_instantiated_directly(self) -> None:
        # matches is abstract; direct instantiation must raise TypeError
        with pytest.raises(TypeError):
            FieldSetter()  # type: ignore[abstract]

    def test_all_six_strategy_classes_inherit_fieldsetter(self) -> None:
        for cls in (
            StructFieldSetter,
            VirtualValueFieldSetter,
            ReadOnlyFieldSetter,
            ListIndexSetter,
            DictKeySetter,
            SequenceSliceSetter,
        ):
            assert issubclass(cls, FieldSetter), f"{cls.__name__} does not inherit FieldSetter"


class TestStructFieldSetterMatches:
    """Spec scenarios for StructFieldSetter.matches."""

    def test_matches_returns_true_for_struct_owner_and_field_segment(self) -> None:
        owner = Struct(x=1)
        assert StructFieldSetter.matches(FieldSegment("x"), owner) is True

    def test_matches_returns_true_for_dict_owner_and_field_segment(self) -> None:
        assert StructFieldSetter.matches(FieldSegment("x"), {"x": 1}) is True

    def test_matches_returns_false_for_index_segment(self) -> None:
        owner = Struct(x=1)
        assert StructFieldSetter.matches(IndexSegment(0), owner) is False


class TestVirtualValueFieldSetterMatches:
    """Spec scenarios for VirtualValueFieldSetter.matches."""

    def test_matches_returns_true_for_virtual_owner_and_field_segment(self) -> None:
        owner = _make_virtual_owner()
        assert VirtualValueFieldSetter.matches(FieldSegment("x"), owner) is True

    def test_matches_returns_false_for_non_virtual_owner(self) -> None:
        owner = Struct(x=1)
        assert VirtualValueFieldSetter.matches(FieldSegment("x"), owner) is False

    def test_preflight_raises_not_implemented_with_virtual_value_in_message(self) -> None:
        # Spec: VirtualValueFieldSetter.preflight raises NotImplementedError with "virtual value"
        owner = _make_virtual_owner()
        place = _make_place(owner, FieldSegment("x"))
        with pytest.raises(NotImplementedError, match="virtual value"):
            VirtualValueFieldSetter().preflight(place, "new", mode="inplace")


class TestReadOnlyFieldSetterMatches:
    """ReadOnlyFieldSetter.matches always returns False (selected via explicit pre-check)."""

    def test_matches_always_returns_false(self) -> None:
        assert ReadOnlyFieldSetter.matches(FieldSegment("x"), Struct(x=1)) is False
        assert ReadOnlyFieldSetter.matches(FieldSegment("x"), {}) is False

    def test_preflight_raises_not_implemented_with_read_only_in_message(self) -> None:
        # Spec: ReadOnlyFieldSetter.preflight raises NotImplementedError with "read-only"
        owner = Struct(x=1)
        place = _make_place(owner, FieldSegment("x"))
        with pytest.raises(NotImplementedError, match="read-only"):
            ReadOnlyFieldSetter().preflight(place, "new", mode="inplace")


class TestListIndexSetterMatches:
    """Spec scenarios for ListIndexSetter.matches."""

    def test_matches_returns_true_for_list_owner_and_index_segment(self) -> None:
        assert ListIndexSetter.matches(IndexSegment(0), [1, 2, 3]) is True

    def test_matches_returns_false_for_tuple_owner(self) -> None:
        # Tuples are not lists
        assert ListIndexSetter.matches(IndexSegment(0), (1, 2, 3)) is False

    def test_matches_returns_false_for_struct_owner(self) -> None:
        assert ListIndexSetter.matches(IndexSegment(0), Struct(x=1)) is False


class TestDictKeySetterMatches:
    """Spec scenarios for DictKeySetter.matches."""

    def test_matches_returns_true_for_dict_and_key_segment(self) -> None:
        assert DictKeySetter.matches(KeySegment("k"), {"k": 1}) is True

    def test_matches_returns_true_for_dict_and_field_segment(self) -> None:
        assert DictKeySetter.matches(FieldSegment("k"), {"k": 1}) is True

    def test_matches_returns_false_for_non_dict(self) -> None:
        assert DictKeySetter.matches(KeySegment("k"), Struct(k=1)) is False


class TestSequenceSliceSetterMatches:
    """Spec scenarios for SequenceSliceSetter.matches."""

    def test_matches_returns_true_for_list_and_slice_segment(self) -> None:
        assert SequenceSliceSetter.matches(SliceSegment(0, 2, None), [1, 2, 3]) is True

    def test_matches_returns_false_for_tuple(self) -> None:
        assert SequenceSliceSetter.matches(SliceSegment(0, 2, None), (1, 2, 3)) is False

    def test_preflight_raises_type_error_for_non_list_owner(self) -> None:
        # Spec: SequenceSliceSetter.preflight raises TypeError with "list" in message
        owner = (1, 2, 3)
        place = _make_place(owner, SliceSegment(0, 2, None))
        with pytest.raises(TypeError, match="list"):
            SequenceSliceSetter().preflight(place, 99, mode="inplace")


class TestStrategyCommitBehaviour:
    """Spec scenarios: commit and preflight behaviours are preserved exactly."""

    def test_struct_field_setter_commit_returns_updated_struct(self) -> None:
        # Spec: StructFieldSetter.commit on a struct place returns the struct with field replaced
        owner = Struct(x=1, y=2)
        segment = FieldSegment("x")
        place = _make_place(owner, segment, current_value=1)

        result = StructFieldSetter().commit(place, 99, mode="inplace")

        assert isinstance(result, Struct)
        assert result.x == 99
        assert result.y == 2
        # original is not mutated
        assert owner.x == 1

    def test_struct_field_setter_commit_does_not_mutate_original(self) -> None:
        owner = Struct(config=Struct(lr=0.1))
        segment = FieldSegment("config")
        new_config = Struct(lr=0.9)
        place = _make_place(owner, segment, current_value=owner.config)

        result = StructFieldSetter().commit(place, new_config, mode="copy")

        assert result.config.lr == 0.9
        assert owner.config.lr == 0.1

    def test_list_index_setter_commit_returns_list_with_element_replaced(self) -> None:
        # Spec: ListIndexSetter.commit on a list place returns the list with element replaced
        owner = [10, 20, 30]
        segment = IndexSegment(1)
        place = _make_place(owner, segment, current_value=20)

        result = ListIndexSetter().commit(place, 99, mode="inplace")

        assert result == [10, 99, 30]
        assert owner == [10, 20, 30]

    def test_dict_key_setter_commit_returns_dict_with_key_replaced(self) -> None:
        owner = {"a": 1, "b": 2}
        segment = KeySegment("a")
        place = _make_place(owner, segment, current_value=1)

        result = DictKeySetter().commit(place, 99, mode="inplace")

        assert result == {"a": 99, "b": 2}
        assert owner == {"a": 1, "b": 2}

    def test_sequence_slice_setter_commit_replaces_slice_elements(self) -> None:
        owner = [0, 1, 2, 3, 4]
        segment = SliceSegment(None, None, 2)
        place = _make_place(owner, segment, current_value=[0, 2, 4])

        result = SequenceSliceSetter().commit(place, 99, mode="inplace")

        assert result == [99, 1, 99, 3, 99]
