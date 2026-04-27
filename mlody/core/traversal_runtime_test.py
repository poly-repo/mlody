"""Tests for the shared traversal runtime helpers."""

from __future__ import annotations

from mlody.common.struct import Struct

from mlody.core.setf import setf_root
from mlody.core.traversal_grammar import (
    FieldSegment,
    IndexSegment,
    KeySegment,
    SliceSegment,
)
from mlody.core.traversal_runtime import (
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

    def test_iter_children_returns_segments_for_struct_list_and_dict(self) -> None:
        struct_children = list(iter_children(Struct(answer=42)))
        list_children = list(iter_children(["a", "b"]))
        dict_children = list(iter_children({"answer": 42, 1: "ignored"}))

        assert struct_children == [(FieldSegment("answer"), 42)]
        assert list_children == [(IndexSegment(0), "a"), (IndexSegment(1), "b")]
        assert dict_children == [(KeySegment("answer"), 42)]

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
