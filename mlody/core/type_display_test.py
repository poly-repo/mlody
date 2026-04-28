"""Unit tests for aggregate type display helpers."""

from __future__ import annotations

from types import SimpleNamespace

from common.python.starlarkish.core.struct import Struct

from mlody.core.type_display import format_type_label, format_value_type_label


def _make_type(
    name: str,
    *,
    type_name: str | None = None,
    root_kind: str | None = None,
    attributes: dict[str, object] | None = None,
) -> Struct:
    return Struct(
        kind="type",
        type=type_name or name,
        name=name,
        _root_kind=root_kind or (type_name or name),
        attributes=attributes or {},
        _allowed_attrs={},
    )


def test_format_type_label_renders_primitive_name() -> None:
    assert format_type_label(_make_type("integer", root_kind="integer")) == "integer"


def test_format_type_label_renders_plain_vector_with_element_type() -> None:
    string_type = _make_type("string", root_kind="string")
    vector_type = _make_type(
        "vector",
        root_kind="vector",
        attributes={"element_type": string_type},
    )

    assert format_type_label(vector_type) == "vector[string]"


def test_format_type_label_renders_named_vector_alias_with_detail() -> None:
    row_type = _make_type("row", root_kind="record")
    dataset_type = _make_type(
        "dataset",
        root_kind="vector",
        attributes={"element_type": row_type},
    )

    assert format_type_label(dataset_type) == "dataset (vector[row])"


def test_format_type_label_renders_plain_tuple_schema() -> None:
    float_type = _make_type("float", root_kind="float")
    tuple_type = _make_type(
        "tuple",
        root_kind="tuple",
        attributes={"_element_types": [float_type, float_type]},
    )

    assert format_type_label(tuple_type) == "tuple[float, float]"


def test_format_type_label_renders_named_tuple_alias_with_detail() -> None:
    float_type = _make_type("float", root_kind="float")
    point_type = _make_type(
        "point",
        root_kind="tuple",
        attributes={"_element_types": [float_type, float_type]},
    )

    assert format_type_label(point_type) == "point (tuple[float, float])"


def test_format_type_label_renders_nested_aggregates_recursively() -> None:
    float_type = _make_type("float", root_kind="float")
    tuple_type = _make_type(
        "tuple",
        root_kind="tuple",
        attributes={"_element_types": [float_type, float_type]},
    )
    vector_type = _make_type(
        "vector",
        root_kind="vector",
        attributes={"element_type": tuple_type},
    )

    assert format_type_label(vector_type) == "vector[tuple[float, float]]"


def test_format_type_label_returns_question_mark_for_unknown_type() -> None:
    assert format_type_label(None) == "?"
    assert format_value_type_label(SimpleNamespace()) == "?"


def test_format_type_label_falls_back_for_missing_aggregate_metadata() -> None:
    assert format_type_label(_make_type("vector", root_kind="vector")) == "vector"
    assert format_type_label(_make_type("tuple", root_kind="tuple")) == "tuple"


def test_format_value_type_label_supports_loose_test_doubles() -> None:
    value = SimpleNamespace(
        type=SimpleNamespace(
            name="dataset",
            _root_kind="vector",
            attributes={"element_type": SimpleNamespace(name="row")},
        )
    )

    assert format_value_type_label(value) == "dataset (vector[row])"
