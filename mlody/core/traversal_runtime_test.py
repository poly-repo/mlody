"""Tests for the shared traversal runtime helpers."""

from __future__ import annotations

import pytest

from mlody.common.struct import Struct

from mlody.core.setf import setf_root
from mlody.core.traversal_grammar import (
    FieldSegment,
    IndexSegment,
    KeySegment,
    SliceSegment,
)
from mlody.core.traversal_runtime import (
    _DICT_ADAPTER,
    _OBJECT_ADAPTER,
    _SEQUENCE_ADAPTER,
    _STRUCT_ADAPTER,
    _VIRTUAL_VALUE_ADAPTER,
    _adapter_for,
    has_named_child,
    iter_children,
    replace_child,
    step_named_child,
    step_segment,
)
from mlody.core.virtual_value import make_virtual_value


_STRING_TYPE = Struct(kind="type", type="string", name="string")
_WORKSPACE_INFO_TYPE = Struct(
    kind="type",
    type="workspace_info",
    name="workspace_info",
    _root_kind="record",
    fields=[
        Struct(name="branch", type=_STRING_TYPE),
        Struct(name="sha", type=_STRING_TYPE),
    ],
)


def _virtual_workspace_info() -> Struct:
    return make_virtual_value(
        value_type=_WORKSPACE_INFO_TYPE,
        label="'info",
        materializer=lambda _value: Struct(branch="main", sha="abc123"),
    )


class TestTraversalRuntime:
    """Requirement: traversal_runtime centralizes runtime access semantics."""

    def test_step_named_child_selects_named_item_from_list(self) -> None:
        items = [
            Struct(name="features", kind="value"),
            Struct(name="labels", kind="value"),
        ]

        result = step_named_child(items, "labels")

        assert getattr(result, "name", None) == "labels"

    def test_step_segment_reads_struct_field(self) -> None:
        result = step_segment(Struct(answer=42), FieldSegment("answer"))

        assert result == 42

    def test_step_segment_reads_dict_key(self) -> None:
        result = step_segment({"answer": 42}, KeySegment("answer"))

        assert result == 42

    def test_step_segment_reads_list_index(self) -> None:
        result = step_segment([10, 20, 30], IndexSegment(1))

        assert result == 20

    def test_step_segment_traverses_virtual_value_declared_field(self) -> None:
        result = step_segment(_virtual_workspace_info(), FieldSegment("branch"))

        assert getattr(result, "kind", None) == "value"
        assert getattr(result, "label", None) == "'info.branch"

    def test_step_segment_materializes_runtime_method_attribute(self) -> None:
        location = Struct(
            kind="location",
            type="inline",
            name="inline",
            methods=Struct(
                info=lambda owner, _enclosing=None: f"location: {owner.name}",
            ),
        )

        result = step_segment(location, FieldSegment("info"))

        assert result == "location: inline"

    def test_iter_children_returns_segments_for_struct_list_and_dict(self) -> None:
        struct_children = list(iter_children(Struct(answer=42)))
        list_children = list(iter_children(["a", "b"]))
        dict_children = list(iter_children({"answer": 42, 1: "ignored"}))

        assert struct_children == [(FieldSegment("answer"), 42)]
        assert list_children == [(IndexSegment(0), "a"), (IndexSegment(1), "b")]
        assert dict_children == [(KeySegment("answer"), 42)]

    def test_iter_children_includes_runtime_method_attributes(self) -> None:
        location = Struct(
            kind="location",
            type="inline",
            name="inline",
            methods=Struct(
                info=lambda owner, _enclosing=None: f"location: {owner.name}",
            ),
        )

        children = list(iter_children(location))

        assert (FieldSegment("info"), "location: inline") in children

    def test_replace_child_updates_struct_list_and_dict_without_mutating_original(self) -> None:
        struct_value = Struct(answer=1)
        list_value = [1, 2, 3]
        dict_value = {"answer": 1}

        updated_struct = replace_child(struct_value, FieldSegment("answer"), 99)
        updated_list = replace_child(list_value, IndexSegment(1), 99)
        updated_dict = replace_child(dict_value, KeySegment("answer"), 99)

        assert updated_struct.answer == 99
        assert struct_value.answer == 1
        assert updated_list == [1, 99, 3]
        assert list_value == [1, 2, 3]
        assert updated_dict == {"answer": 99}
        assert dict_value == {"answer": 1}

    def test_setf_root_updates_slice_selection_via_shared_runtime(self) -> None:
        root = Struct(items=[0, 1, 2, 3, 4])

        updated = setf_root(root, ".items[::2]", 42)

        assert updated.items == [42, 1, 42, 3, 42]
        assert root.items == [0, 1, 2, 3, 4]

    def test_replace_child_updates_slice_selection_without_mutating_original(self) -> None:
        values = [0, 1, 2, 3, 4]

        updated = replace_child(values, SliceSegment(None, None, 2), 42)

        assert updated == [42, 1, 42, 3, 42]
        assert values == [0, 1, 2, 3, 4]


class TestAdapterForDispatch:
    """Requirement: _adapter_for centralises traversal adapter selection (F3)."""

    def test_adapter_for_returns_virtual_value_adapter_for_virtual_value(self) -> None:
        # (a) VirtualValue adapter selected for virtual values
        _STRING_TYPE = Struct(kind="type", type="string", name="string")
        _WORKSPACE_INFO_TYPE = Struct(
            kind="type",
            type="workspace_info",
            name="workspace_info",
            _root_kind="record",
            fields=[Struct(name="branch", type=_STRING_TYPE)],
        )
        virtual_value = make_virtual_value(
            value_type=_WORKSPACE_INFO_TYPE,
            label="'info",
            materializer=lambda _v: Struct(branch="main"),
        )

        adapter = _adapter_for(virtual_value)

        assert adapter is _VIRTUAL_VALUE_ADAPTER

    def test_adapter_for_returns_struct_adapter_for_struct_instance(self) -> None:
        # (b) Struct adapter selected for Struct instances
        adapter = _adapter_for(Struct(x=1))

        assert adapter is _STRUCT_ADAPTER

    def test_adapter_for_returns_dict_adapter_for_dict(self) -> None:
        # (c) Dict adapter selected for plain dicts
        adapter = _adapter_for({"key": "val"})

        assert adapter is _DICT_ADAPTER

    def test_adapter_for_returns_sequence_adapter_for_list(self) -> None:
        # (d) Sequence adapter selected for list
        adapter = _adapter_for([1, 2, 3])

        assert adapter is _SEQUENCE_ADAPTER

    def test_adapter_for_returns_sequence_adapter_for_tuple(self) -> None:
        # (d) Sequence adapter selected for tuple
        adapter = _adapter_for((1, 2, 3))

        assert adapter is _SEQUENCE_ADAPTER

    def test_adapter_for_returns_object_adapter_for_plain_object(self) -> None:
        # (e) Object adapter is the fallback for plain Python objects
        adapter = _adapter_for(object())

        assert adapter is _OBJECT_ADAPTER

    def test_step_named_child_on_struct_returns_correct_field(self) -> None:
        # (f) step_named_child delegates correctly for struct
        result = step_named_child(Struct(answer=42), "answer")

        assert result == 42

    def test_iter_children_on_list_returns_index_segment_tuples(self) -> None:
        # (g) iter_children returns (IndexSegment(i), v) for a list
        result = iter_children([10, 20, 30])

        assert result == (
            (IndexSegment(0), 10),
            (IndexSegment(1), 20),
            (IndexSegment(2), 30),
        )

    def test_has_named_child_on_dict_with_key_returns_true(self) -> None:
        # (h) has_named_child returns True when key exists in dict
        assert has_named_child({"x": 1}, "x") is True

    def test_step_named_child_on_struct_with_missing_field_raises_attribute_error(self) -> None:
        # (i) missing struct field raises AttributeError
        with pytest.raises(AttributeError):
            step_named_child(Struct(x=1), "nonexistent_field")

    def test_step_segment_on_unsupported_segment_type_raises_not_implemented(self) -> None:
        # (j) unsupported segment type raises NotImplementedError
        class _UnrecognisedSegment:
            pass

        with pytest.raises(NotImplementedError):
            step_segment(Struct(x=1), _UnrecognisedSegment())

    def test_replace_child_on_dict_via_field_segment_returns_updated_dict_without_mutating(self) -> None:
        # (k) replace_child on dict via FieldSegment returns updated dict, original unchanged
        original = {"x": 1}

        result = replace_child(original, FieldSegment("x"), 99)

        assert result == {"x": 99}
        assert original == {"x": 1}
