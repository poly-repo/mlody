"""Integration tests for mlody/common/values.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
import mlody
from mlody.core.value_context_validation import (
    ContextRestrictedValueValidationError,
    validate_context_restricted_values_evaluator,
)

assert mlody.__name__ == "mlody"

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_FRESHNESS_MLODY = (_THIS_DIR / "freshness.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_REPRESENTATION_MLODY = (_THIS_DIR / "representation.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/freshness.mlody": _FRESHNESS_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/representation.mlody": _REPRESENTATION_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
}


def _eval(extra_mlody: str) -> Evaluator:
    script = (
        'load("//mlody/common/types.mlody")\n'
        'load("//mlody/common/locations.mlody")\n'
        'load("//mlody/common/representation.mlody")\n'
        'load("//mlody/common/values.mlody")\n'
        + dedent(extra_mlody)
    )
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def _result(ev: Evaluator) -> object:
    return ev._module_globals[ev.root_path / "test.mlody"]["result"]


def _resolve_and_validate(ev: Evaluator) -> None:
    ev.resolve()
    validate_context_restricted_values_evaluator(ev)


# ---------------------------------------------------------------------------
# TC-001: value() with direct structs registers with kind="value"
# ---------------------------------------------------------------------------


def test_value_with_direct_structs_registers_correctly() -> None:
    """TC-001: value(name='x', type=integer(), location=s3()) → kind='value'."""
    ev = _eval('value(name="x", type=integer(), location=s3())')
    assert "x" in ev.registry.values.by_name
    v = ev.registry.values.by_name["x"]
    assert v.kind == "value"
    assert v.name == "x"


def test_value_stores_type_and_location_name() -> None:
    """TC-001: value struct holds .type.name and .location.name."""
    ev = _eval('value(name="x", type=integer(), location=s3())')
    v = ev.registry.values.by_name["x"]
    assert v.type.name == "integer"
    assert v.location.name == "s3"


# ---------------------------------------------------------------------------
# TC-002: string label for type is resolved
# ---------------------------------------------------------------------------


def test_value_string_type_label_resolves_to_type_struct() -> None:
    """TC-002: type='integer' (string) resolves to the integer type struct."""
    ev = _eval('value(name="y", type="integer", location=s3())')
    v = ev.registry.values.by_name["y"]
    assert v.type.kind == "type"
    assert v.type.name == "integer"


# ---------------------------------------------------------------------------
# TC-003: string label for location is resolved
# ---------------------------------------------------------------------------


def test_value_string_location_label_resolves_to_location_struct() -> None:
    """TC-003: location='s3' (string) resolves to the s3 location struct."""
    ev = _eval('value(name="z", type=integer(), location="s3")')
    v = ev.registry.values.by_name["z"]
    assert v.location.kind == "location"
    assert v.location.name == "s3"


def test_value_string_freshness_label_resolves_to_freshness_struct() -> None:
    """freshness='always' (string) resolves to the always freshness struct."""
    ev = _eval('value(name="z", type=integer(), freshness="always")')
    v = ev.registry.values.by_name["z"]
    assert v.freshness.kind == "freshness"
    assert v.freshness.name == "always"


# ---------------------------------------------------------------------------
# TC-004: constrained type struct is stored
# ---------------------------------------------------------------------------


def test_value_stores_constrained_type_struct() -> None:
    """TC-004: type=integer(max=100) stores the constrained struct."""
    ev = _eval('value(name="bounded", type=integer(max=100), location=s3())')
    v = ev.registry.values.by_name["bounded"]
    assert v.type.kind == "type"
    assert v.type.attributes.get("max") == 100


# ---------------------------------------------------------------------------
# TC-005: constrained location struct is stored
# ---------------------------------------------------------------------------


def test_value_stores_constrained_location_struct() -> None:
    """TC-005: location=s3(bucket='prod') stores the constrained struct."""
    ev = _eval('value(name="prod_data", type=integer(), location=s3(bucket="prod"))')
    v = ev.registry.values.by_name["prod_data"]
    assert v.location.kind == "location"
    assert v.location.attributes.get("bucket") == "prod"


def test_value_stores_constrained_freshness_struct() -> None:
    """freshness=ttl(duration='P1D') stores the constrained struct."""
    ev = _eval('value(name="prod_data", type=integer(), freshness=ttl(duration="P1D"))')
    v = ev.registry.values.by_name["prod_data"]
    assert v.freshness.kind == "freshness"
    assert v.freshness.attributes.get("duration") == "P1D"


# ---------------------------------------------------------------------------
# TC-006: unknown type string raises NameError
# ---------------------------------------------------------------------------


def test_value_unknown_type_string_raises_name_error() -> None:
    """TC-006: type='nonexistent' raises NameError."""
    with pytest.raises(NameError):
        _eval('value(name="bad", type="nonexistent", location=s3())')


# ---------------------------------------------------------------------------
# TC-007: unknown location string raises NameError
# ---------------------------------------------------------------------------


def test_value_unknown_location_string_raises_name_error() -> None:
    """TC-007: location='nonexistent' raises NameError."""
    with pytest.raises(NameError):
        _eval('value(name="bad", type=integer(), location="nonexistent")')


def test_value_unknown_freshness_string_raises_name_error() -> None:
    """freshness='nonexistent' raises NameError."""
    with pytest.raises(NameError):
        _eval('value(name="bad", type=integer(), freshness="nonexistent")')


# ---------------------------------------------------------------------------
# TC-008: wrong type for type attr raises TypeError
# ---------------------------------------------------------------------------


def test_value_location_struct_as_type_raises_type_error() -> None:
    """TC-008: passing a location struct as type raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="bad", type=s3(), location=s3())')


# ---------------------------------------------------------------------------
# TC-009: wrong type for location attr raises TypeError
# ---------------------------------------------------------------------------


def test_value_type_struct_as_location_raises_type_error() -> None:
    """TC-009: passing a type struct as location raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="bad", type=integer(), location=integer())')


def test_value_location_struct_as_freshness_raises_type_error() -> None:
    """Passing a location struct as freshness raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="bad", type=integer(), freshness=s3())')


# ---------------------------------------------------------------------------
# TC-010: freshly registered value has an empty _lineage list
# ---------------------------------------------------------------------------


def test_value_has_empty_lineage_on_creation() -> None:
    """TC-010: a new value has _lineage == []."""
    ev = _eval('value(name="v", type=integer(), location=s3())')
    v = ev.registry.values.by_name["v"]
    assert v._lineage == []


def test_value_lineage_is_a_list() -> None:
    """TC-010: _lineage is a list, not None or missing."""
    ev = _eval('value(name="v", type=integer(), location=s3())')
    v = ev.registry.values.by_name["v"]
    assert isinstance(v._lineage, list)


# ---------------------------------------------------------------------------
# TC-011: value() allows partial declarations (type/location optional)
# ---------------------------------------------------------------------------


def test_value_allows_missing_location() -> None:
    ev = _eval('value(name="v", type=integer())')
    v = ev.registry.values.by_name["v"]
    assert v.type.kind == "type"
    assert v.location.type == "inline"
    assert v.freshness.type == "always"


def test_value_allows_missing_type() -> None:
    ev = _eval('value(name="v", location=s3())')
    v = ev.registry.values.by_name["v"]
    assert v.type.name == "nothing"
    assert v.location.kind == "location"
    assert v.freshness.type == "always"


def test_both_defaults_applied() -> None:
    ev = _eval('value(name="v")')
    v = ev.registry.values.by_name["v"]
    assert v.type.name == "nothing"
    assert v.location.type == "inline"
    assert v.freshness.type == "always"


def test_inline_location_with_data() -> None:
    ev = _eval('value(name="v", location=inline(data="hello"))')
    v = ev.registry.values.by_name["v"]
    assert v.location.type == "inline"
    assert v.location.data == "hello"
    assert "data" not in v.location.attributes


# ---------------------------------------------------------------------------
# TC-012: value() accepts optional default of any Starlark builtin type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_expr", "default_expr", "expected"),
    [
        ("integer()", "1", 1),
        ("float()", "3.14", 3.14),
        ("string()", '"hello"', "hello"),
        ("bool()", "True", True),
        ("opaque()", "[1, 2, 3]", [1, 2, 3]),
    ],
)
def test_value_stores_default_builtin_types(
    type_expr: str, default_expr: str, expected: object
) -> None:
    ev = _eval(
        f'value(name="v", type={type_expr}, location=inline(), default={default_expr})'
    )
    v = ev.registry.values.by_name["v"]
    assert v.default == expected


def test_value_stores_dict_literal_default() -> None:
    ev = _eval('value(name="v", type=opaque(), location=inline(), default={"k": "v"})')
    v = ev.registry.values.by_name["v"]
    # In starlarkish, dict literals are represented as Struct values.
    assert getattr(v.default, "k", None) == "v"


def test_value_stores_tuple_literal_default() -> None:
    ev = _eval('value(name="v", type=opaque(), location=inline(), default=(1, 2))')
    v = ev.registry.values.by_name["v"]
    # Tuples are normalized to list in runtime values.
    assert v.default == [1, 2]


# ---------------------------------------------------------------------------
# TC-013 (5.1): value() with representation=json() carries representation struct
# ---------------------------------------------------------------------------


def test_value_with_representation_json_carries_representation_struct() -> None:
    """5.1: value(representation=json()) → result.representation.kind == 'representation'
    and result.representation.name == 'json'.
    """
    ev = _eval('value(name="x", type=integer(), location=s3(), representation=json())')
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.kind == "representation"
    assert v.representation.name == "json"


def test_value_with_representation_text_defaults_markup_to_none() -> None:
    ev = _eval('value(name="x", type=integer(), location=s3(), representation=text())')
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.kind == "representation"
    assert v.representation.name == "text"
    assert v.representation.markup == "none"


def test_value_with_representation_text_accepts_markdown_markup() -> None:
    ev = _eval(
        'value(name="x", type=integer(), location=s3(), representation=text(markup="markdown"))'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "text"
    assert v.representation.markup == "markdown"


def test_value_with_representation_text_invalid_markup_raises_value_error() -> None:
    with pytest.raises(ValueError, match="text\\(markup"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=text(markup="html"))'
        )


def test_value_with_representation_parquet_defaults() -> None:
    ev = _eval(
        'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
        'value(name="x", type=integer(), location=s3(), representation=parquet(schema=row_schema()))'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "parquet"
    assert v.representation.multifile is False
    assert v.representation.schema.name == "row_schema"
    assert "min_length" not in v.representation.attributes
    assert "max_length" not in v.representation.attributes
    assert "total_min_length" not in v.representation.attributes
    assert "total_max_length" not in v.representation.attributes


def test_value_with_representation_parquet_supports_string_schema_ref() -> None:
    ev = _eval(
        'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
        'value(name="x", type=integer(), location=s3(), representation=parquet(schema="row_schema"))'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "parquet"
    assert v.representation.schema.name == "row_schema"


def test_value_with_representation_parquet_accepts_bounds_and_multifile() -> None:
    ev = _eval(
        'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
        'value(\n'
        '  name="x",\n'
        '  type=integer(),\n'
        '  location=s3(),\n'
        '  representation=parquet(\n'
        '    schema=row_schema(),\n'
        '    multifile=True,\n'
        '    min_length=1,\n'
        '    max_length=10,\n'
        '    total_min_length=2,\n'
        '    total_max_length=20,\n'
        '  ),\n'
        ')\n'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "parquet"
    assert v.representation.multifile is True
    assert v.representation.min_length == 1
    assert v.representation.max_length == 10
    assert v.representation.total_min_length == 2
    assert v.representation.total_max_length == 20


def test_value_with_representation_parquet_rejects_non_record_schema() -> None:
    with pytest.raises(TypeError, match="record typedef"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=parquet(schema=integer()))'
        )


def test_value_with_representation_parquet_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unknown type"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=parquet(schema="missing_schema"))'
        )


# ---------------------------------------------------------------------------
# TC: value() validates default= against type predicate
# ---------------------------------------------------------------------------


def test_value_default_invalid_for_typed_value_raises() -> None:
    with pytest.raises(TypeError):
        _eval('value(name="x", type=commit(), default="xxxx")')


def test_value_default_valid_sha_accepted() -> None:
    valid_sha = "a" * 40
    canonical_sha = "b" * 40
    import common.python.starlarkish.evaluator.evaluator as _ev_mod
    from common.python.starlarkish.core.struct import struct as _struct
    from unittest.mock import patch

    mock_python = _struct(
        **{**_ev_mod.PYTHON_SPECIFIC_BUILTINS.as_mapping(), "expand_commit_sha": lambda _: canonical_sha}
    )
    with patch.dict(_ev_mod.SAFE_BUILTINS, {"python": mock_python}):
        ev = _eval(f'value(name="x", type=commit(), default="{valid_sha}")')
    v = ev.registry.values.by_name["x"]
    assert v.default == canonical_sha


def test_value_default_opaque_type_passes_through() -> None:
    ev = _eval('value(name="x", type=opaque(), default="any-value")')
    v = ev.registry.values.by_name["x"]
    assert v.default == "any-value"


def test_value_no_default_untyped_passes_through() -> None:
    ev = _eval('value(name="x")')
    v = ev.registry.values.by_name["x"]
    assert v.default is None


def test_value_with_representation_parquet_rejects_invalid_file_bounds() -> None:
    with pytest.raises(ValueError, match="min_length"):
        _eval(
            'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
            'value(name="x", type=integer(), location=s3(), representation=parquet(schema=row_schema(), min_length=5, max_length=1))'
        )


def test_value_with_representation_parquet_rejects_invalid_total_bounds() -> None:
    with pytest.raises(ValueError, match="total_min_length"):
        _eval(
            'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
            'value(name="x", type=integer(), location=s3(), representation=parquet(schema=row_schema(), total_min_length=10, total_max_length=1))'
        )


def test_value_with_representation_csv_defaults() -> None:
    ev = _eval('value(name="x", type=integer(), location=s3(), representation=csv())')
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "csv"
    assert v.representation.separator == ","
    assert v.representation.header_required is True
    assert v.representation.multifile is False
    assert "schema" not in v.representation.attributes


def test_value_with_representation_csv_accepts_optional_record_schema() -> None:
    ev = _eval(
        'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
        'value(name="x", type=integer(), location=s3(), representation=csv(schema=row_schema()))'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "csv"
    assert v.representation.schema.name == "row_schema"


def test_value_with_representation_csv_accepts_string_schema_and_options() -> None:
    ev = _eval(
        'typedef(name="row_schema", base=record(fields=[field(name="id", type=integer())]))\n'
        'value(\n'
        '  name="x",\n'
        '  type=integer(),\n'
        '  location=s3(),\n'
        '  representation=csv(schema="row_schema", separator="|", header_required=False, multifile=True),\n'
        ')\n'
    )
    v = ev.registry.values.by_name["x"]
    assert v.representation is not None
    assert v.representation.name == "csv"
    assert v.representation.schema.name == "row_schema"
    assert v.representation.separator == "|"
    assert v.representation.header_required is False
    assert v.representation.multifile is True


def test_value_with_representation_csv_rejects_non_record_schema() -> None:
    with pytest.raises(TypeError, match="record typedef"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=csv(schema=integer()))'
        )


def test_value_with_representation_csv_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unknown type"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=csv(schema="missing_schema"))'
        )


def test_value_with_representation_csv_rejects_empty_separator() -> None:
    with pytest.raises(ValueError, match="separator"):
        _eval(
            'value(name="x", type=integer(), location=s3(), representation=csv(separator=""))'
        )


# ---------------------------------------------------------------------------
# TC-014 (5.2): value() without representation has representation=None
# ---------------------------------------------------------------------------


def test_value_without_representation_has_representation_none() -> None:
    """5.2: value() without representation attr → result.representation is None."""
    ev = _eval('value(name="x", type=integer(), location=s3())')
    v = ev.registry.values.by_name["x"]
    assert v.representation is None


# ---------------------------------------------------------------------------
# TC-015 (5.3): value() with wrong-kind representation raises TypeError
# ---------------------------------------------------------------------------


def test_value_with_wrong_kind_representation_raises_type_error() -> None:
    """5.3: value(representation=posix()) raises TypeError naming kind 'representation'."""
    with pytest.raises(TypeError, match="representation"):
        _eval('value(name="x", type=integer(), location=s3(), representation=posix())')


# ---------------------------------------------------------------------------
# TC-016: source resolution — string, value struct, wrong kind, absent
# ---------------------------------------------------------------------------


def test_value_source_string_label_stored_verbatim() -> None:
    """TC-016a: source= string is a mlody label; stored verbatim, not looked up."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", type=string(), location=s3(), source=":upstream")\n'
    )
    src = ev.registry.values.by_name["v"].source
    assert src == ":upstream"


def test_value_source_unknown_string_stored_verbatim() -> None:
    """TC-016a: unknown label string is kept as-is (resolved lazily by DAG builder)."""
    ev = _eval('value(name="v", type=integer(), location=s3(), source=":nonexistent")')
    assert ev.registry.values.by_name["v"].source == ":nonexistent"


def test_value_source_as_value_struct() -> None:
    """TC-016b: source=<value struct> is stored as the value struct."""
    ev = _eval(
        'upstream = value(name="upstream", type=integer(), location=s3())\n'
        'value(name="downstream", type=string(), location=s3(), source=upstream)\n'
    )
    src = ev.registry.values.by_name["downstream"].source
    assert src.kind == "value"
    assert src.name == "upstream"


def test_value_source_wrong_kind_raises_type_error() -> None:
    """TC-016c: source=s3() (wrong kind) raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="v", type=integer(), location=s3(), source=s3())')


def test_value_source_defaults_to_none() -> None:
    """TC-016d: value() without source has source=None."""
    ev = _eval('value(name="v", type=integer(), location=s3())')
    assert ev.registry.values.by_name["v"].source is None


def test_value_source_stores_context_policy() -> None:
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", type=string(), location=s3(), source=":upstream")\n'
    )
    value = ev.registry.values.by_name["v"]
    assert value._context_attr_policies["source"] == (
        "standalone",
        "task.inputs",
        "task.config",
        "task.action.inputs",
        "task.action.config",
    )


def test_value_source_is_valid_when_standalone() -> None:
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", type=string(), location=s3(), source=":upstream")\n'
    )

    _resolve_and_validate(ev)


# ---------------------------------------------------------------------------
# TC-017: value() source with @sql suffix auto-produces derived location
# Traces to openspec/changes/value-source-query/specs/derived-location/spec.md
# ---------------------------------------------------------------------------


def test_value_source_with_sql_suffix_produces_derived_location() -> None:
    """TC-017a: source with @sql suffix constructs a derived location automatically."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    assert v.location is not None
    assert v.location.type == "derived"


def test_value_source_with_sql_suffix_derived_location_has_source_ref() -> None:
    """TC-017b: derived location source_ref matches the source label (without query)."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    loc = v.location
    assert loc.attributes.get("source_ref") == ":upstream"


def test_value_source_with_sql_suffix_derived_location_has_sql_fragment() -> None:
    """TC-017c: derived location sql_fragment carries the WHERE clause."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    loc = v.location
    assert loc.attributes.get("sql_fragment") == "WHERE split='train'"


def test_value_source_with_sql_suffix_derived_location_has_duckdb_dialect() -> None:
    """TC-017d: derived location dialect is 'duckdb'."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    loc = v.location
    assert loc.attributes.get("dialect") == "duckdb"


def test_value_source_with_sql_suffix_derived_location_has_output_path() -> None:
    """TC-017e: derived location output_path is a non-empty string ending in .parquet."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    loc = v.location
    output_path = loc.attributes.get("output_path")
    assert isinstance(output_path, str)
    assert output_path.endswith(".parquet")


def test_value_source_with_sql_suffix_deterministic_output_path() -> None:
    """TC-017f: identical inputs produce identical output path."""
    import hashlib as _hashlib
    from pathlib import Path as _Path

    source_label = ":upstream"
    dialect = "duckdb"
    sql_fragment = "WHERE split='train'"
    raw = f"{source_label}:{dialect}:{sql_fragment}"
    expected_hash = _hashlib.sha256(raw.encode()).hexdigest()[:40]
    expected_path = str(_Path.home() / ".cache" / "mlody" / "derived" / f"{expected_hash}.parquet")

    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    v = ev.registry.values.by_name["v"]
    assert v.location.attributes.get("output_path") == expected_path


def test_value_source_with_sql_suffix_different_fragment_different_path() -> None:
    """TC-017g: different sql_fragment produces different output path."""
    ev1 = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'train\']")\n'
    )
    ev2 = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source=":upstream[@sql WHERE split=\'test\']")\n'
    )
    path1 = ev1.registry.values.by_name["v"].location.attributes.get("output_path")
    path2 = ev2.registry.values.by_name["v"].location.attributes.get("output_path")
    assert path1 != path2


def test_value_explicit_location_with_sql_source_raises_value_error() -> None:
    """TC-017h: explicit location= alongside query-bearing source raises ValueError."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        _eval(
            'value(name="upstream", type=integer(), location=s3())\n'
            'value(name="v", location=s3(), source=":upstream[@sql SELECT *]")\n'
        )


def test_value_source_without_sql_suffix_location_unaffected() -> None:
    """TC-017i: source without @sql suffix gets inline default location."""
    ev = _eval(
        'value(name="upstream", type=integer(), location=s3())\n'
        'value(name="v", source="upstream")\n'
    )
    v = ev.registry.values.by_name["v"]
    assert v.location.type == "inline"


def test_value_sql_derived_inherits_type_from_record_source() -> None:
    """Regression: SQL-derived value must inherit record type from source.

    Previously, source_ref was a lazy string so python.getattr(source_ref, "type")
    returned None, leaving the derived value with type=None and breaking field traversal.
    """
    ev = _eval(
        'typedef(name="splits", base=record(fields=[\n'
        '  field(name="train", type=integer()),\n'
        '  field(name="valid", type=integer()),\n'
        ']))\n'
        'value(name="base", type=splits(), location=s3())\n'
        'value(name="derived", source=":base[@sql WHERE Bald=True]")\n'
    )
    v = ev.registry.values.by_name["derived"]
    assert v.type is not None
    assert v.type.name == "splits"


def test_value_sql_derived_source_paths_extracted_from_source_location() -> None:
    """Regression: SQL-derived location source_paths must reflect the source's location path.

    Previously, source_ref was a lazy string so _source_loc was None and source_paths=[].
    """
    ev = _eval(
        'value(name="base", type=integer(), location=posix(path="data/*.parquet"))\n'
        "value(name=\"derived\", source=\":base[@sql WHERE split='train']\")\n"
    )
    loc = ev.registry.values.by_name["derived"].location
    assert loc is not None
    assert loc.type == "derived"
    assert loc.attributes.get("source_paths") == ["data/*.parquet"]


def test_value_query_source_is_valid_when_standalone() -> None:
    ev = _eval(
        'value(name="base", type=integer(), location=posix(path="data/*.parquet"))\n'
        "value(name=\"derived\", source=\":base[@sql WHERE split='train']\")\n"
    )

    _resolve_and_validate(ev)
    assert ev.registry.values.by_name["derived"].location.type == "derived"


def test_value_remote_csv_source_with_sql_suffix_produces_derived_location() -> None:
    ev = _eval(
        'typedef(name="employee", base=record(fields=[\n'
        '  field(name="name", type=string()),\n'
        '  field(name="salary", type=integer()),\n'
        ']))\n'
        'value(name="raw_employees", type=vector(element_type=":employee"), '
        'location=remote(uri="https://example.com/employees.csv"), representation=csv())\n'
        'value(name="high_paid", source=":raw_employees[@sql WHERE salary > 100000]")\n'
    )

    _resolve_and_validate(ev)
    assert ev.registry.values.by_name["high_paid"].location.type == "derived"


def test_value_remote_csv_query_source_stashes_hidden_source_value() -> None:
    ev = _eval(
        'typedef(name="employee", base=record(fields=[\n'
        '  field(name="name", type=string()),\n'
        '  field(name="salary", type=integer()),\n'
        ']))\n'
        'value(name="raw_employees", type=vector(element_type=":employee"), '
        'location=remote(uri="https://example.com/employees.csv"), representation=csv())\n'
        'value(name="high_paid", source=":raw_employees[@sql WHERE salary > 100000]")\n'
    )

    value = ev.registry.values.by_name["high_paid"]
    assert value.source == ":raw_employees"
    assert value._source_value.name == "raw_employees"
    assert value._source_value.location.type == "remote"


def test_value_plain_source_stashes_hidden_source_value_when_resolvable() -> None:
    ev = _eval(
        'value(name="raw_employees", type=integer(), location=s3())\n'
        'value(name="raw_employees_local", type=integer(), '
        'location=posix(path="data/raw_employees.csv"), source=":raw_employees")\n'
    )

    value = ev.registry.values.by_name["raw_employees_local"]
    assert value.source == ":raw_employees"
    assert value._source_value.name == "raw_employees"
    assert value._source_value.location.type == "s3"


def test_value_plain_forward_source_does_not_require_lookup_success() -> None:
    ev = _eval(
        'value(name="raw_employees_local", type=integer(), '
        'location=posix(path="data/raw_employees.csv"), source=":raw_employees")\n'
        'value(name="raw_employees", type=integer(), location=s3())\n'
    )

    value = ev.registry.values.by_name["raw_employees_local"]
    assert value.source == ":raw_employees"
    assert not hasattr(value, "_source_value")


def test_value_stores_group_and_context_policy() -> None:
    ev = _eval('value(name="artifact", type=string(), location=s3(), group="train")')
    value = ev.registry.values.by_name["artifact"]
    assert value.group == "train"
    assert value._context_attr_policies == {"group": ("task.outputs",)}


def test_value_stores_constraint_and_context_policy() -> None:
    ev = _eval('value(name="cfg", type=string(), location=inline(), constraint="x > 0")')
    value = ev.registry.values.by_name["cfg"]
    assert value.constraint == "x > 0"
    assert value._context_attr_policies == {
        "constraint": ("task.config", "task.action.config")
    }


def test_value_without_unit_defaults_to_none() -> None:
    ev = _eval('value(name="distance", type=float(), location=inline())')
    value = ev.registry.values.by_name["distance"]
    assert value.unit is None


def test_value_parses_unit_for_numeric_type() -> None:
    ev = _eval('value(name="distance", type=float(), location=inline(), unit="m / s")')
    value = ev.registry.values.by_name["distance"]
    assert value.unit is not None
    assert value.unit.to_string() == "m / s"


def test_value_parses_unit_for_numeric_typedef() -> None:
    ev = _eval(
        'typedef(name="distance", base=float(min=0.0))\n'
        'value(name="run", type=distance(), location=inline(), unit="km")'
    )
    value = ev.registry.values.by_name["run"]
    assert value.unit is not None
    assert value.unit.to_string() == "km"


def test_value_group_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        _eval("value(name='artifact', type=string(), location=s3(), group=1)")


def test_value_constraint_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        _eval("value(name='cfg', type=string(), location=inline(), constraint=1)")


def test_value_unit_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        _eval("value(name='distance', type=float(), location=inline(), unit=1)")


def test_value_unit_rejects_non_numeric_type() -> None:
    with pytest.raises(TypeError, match="numeric value types"):
        _eval("value(name='label', type=string(), location=inline(), unit='m')")


def test_value_unit_rejects_invalid_unit_string() -> None:
    with pytest.raises(ValueError, match="Invalid unit"):
        _eval("value(name='distance', type=float(), location=inline(), unit='not-a-unit')")


def test_value_with_contextual_attr_requires_materialized_context() -> None:
    ev = _eval('value(name="artifact", type=string(), location=s3(), group="train")')

    with pytest.raises(ContextRestrictedValueValidationError) as exc_info:
        _resolve_and_validate(ev)

    violation = exc_info.value.violations[0]
    assert violation.value_name == "artifact"
    assert violation.attr_name == "group"
    assert violation.actual_context == "standalone"
    assert violation.allowed_contexts == ("task.outputs",)


def test_value_attaches_declared_entity_type() -> None:
    ev = _eval('value(name="artifact", type=string(), location=inline())')

    value = ev.registry.values.by_name["artifact"]
    assert value._entity_type.name == "mlody-value"


def test_value_quantity_string_default_same_unit() -> None:
    ev = _eval(
        'value(name="speed", type=float(), unit="m/s", default="3m/s")'
    )
    value = ev.registry.values.by_name["speed"]
    assert value.default == pytest.approx(3.0)


def test_value_quantity_string_default_converts_unit() -> None:
    ev = _eval(
        'value(name="speed", type=float(), unit="m/s", default="3600m/h")'
    )
    value = ev.registry.values.by_name["speed"]
    assert value.default == pytest.approx(1.0)


def test_value_quantity_string_default_incompatible_unit_raises() -> None:
    with pytest.raises(ValueError, match="Cannot convert"):
        _eval('value(name="mass", type=float(), unit="kg", default="3m/s")')


def test_value_bare_number_default_with_unit_still_accepted() -> None:
    ev = _eval(
        'value(name="speed", type=float(), unit="m/s", default=3.0)'
    )
    value = ev.registry.values.by_name["speed"]
    assert value.default == pytest.approx(3.0)
