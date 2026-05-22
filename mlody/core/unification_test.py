"""Unit tests for mlody/core/unification.py.

Every test traces back to a named scenario in the mm-pattern-unification spec.
No evaluator instance is needed — unify() is a pure Python function.
"""

from __future__ import annotations

import pytest

from common.python.starlarkish.core.struct import Struct
from mlody.core.unification import unify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s(**kwargs: object) -> Struct:
    """Shorthand for building test Struct instances."""
    return Struct(**kwargs)  # type: ignore[arg-type]


# Fixed mm pattern constants that mirror what mm.mlody produces.
ANY = _s(kind="mm_any")


def var(name: str) -> Struct:
    return _s(kind="mm_var_pattern", var_name=name)


def literal(v: object) -> Struct:
    return _s(kind="mm_literal_pattern", value=v)


def or_(*patterns: object) -> Struct:
    return _s(kind="mm_or_pattern", patterns=list(patterns))


def T(type_name: str) -> Struct:
    return _s(kind="mm_scalar_pattern", type_name=type_name)


def repr_pattern(repr_name: str) -> Struct:
    return _s(kind="mm_repr_pattern", repr_name=repr_name)


def entity_pattern(
    entity_kind: str,
    entity_name: str,
    field_patterns: dict[str, object] | None = None,
) -> Struct:
    return _s(
        kind="mm_entity_pattern",
        entity_kind=entity_kind,
        entity_name=entity_name,
        field_patterns=field_patterns or {},
    )


# Pre-built repr-arg structs used by repr tests.
JSON_ARG = _s(kind="representation", name="json")
PARQUET_ARG = _s(kind="representation", name="parquet")

# Pre-built type-arg structs used by scalar pattern tests.
STRING_TYPE = _s(kind="type", type_name="string", name="string")
INTEGER_TYPE = _s(kind="type", type_name="integer", name="integer")


# ---------------------------------------------------------------------------
# Base cases — bare literal equality (task 4.1)
# ---------------------------------------------------------------------------


def test_unify_equal_strings_returns_empty_dict() -> None:
    """Bare literal equality: unify('json', 'json') → {}."""
    assert unify("json", "json") == {}


def test_unify_unequal_strings_returns_none() -> None:
    """Bare literal inequality: unify('json', 'parquet') → None."""
    assert unify("json", "parquet") is None


def test_unify_equal_integers_returns_empty_dict() -> None:
    """Bare integer equality: unify(42, 42) → {}."""
    assert unify(42, 42) == {}


def test_unify_unequal_integers_returns_none() -> None:
    """Bare integer inequality: unify(42, 43) → None."""
    assert unify(42, 43) is None


def test_unify_none_with_none_returns_empty_dict() -> None:
    """None unifies with None as a bare literal."""
    assert unify(None, None) == {}


def test_unify_none_with_string_returns_none() -> None:
    """None does not unify with a string."""
    assert unify(None, "x") is None


# ---------------------------------------------------------------------------
# mm.ANY wildcard (task 4.1)
# ---------------------------------------------------------------------------


def test_unify_any_with_string_returns_empty_dict() -> None:
    """mm.ANY matches anything: unify(mm.ANY, 'anything') → {}."""
    assert unify(ANY, "anything") == {}


def test_unify_both_any_returns_empty_dict() -> None:
    """Both mm.ANY: unify(mm.ANY, mm.ANY) → {}."""
    assert unify(ANY, ANY) == {}


def test_unify_string_with_any_returns_empty_dict() -> None:
    """mm.ANY symmetry: unify('x', mm.ANY) → {}."""
    assert unify("x", ANY) == {}


def test_unify_any_with_struct_returns_empty_dict() -> None:
    """mm.ANY matches a Struct without producing bindings."""
    some_struct = _s(kind="type", name="string")
    assert unify(ANY, some_struct) == {}


def test_unify_any_with_none_returns_empty_dict() -> None:
    """mm.ANY matches None (nameless discard)."""
    assert unify(ANY, None) == {}


# ---------------------------------------------------------------------------
# mm.var — bind and occurrence check (task 4.2)
# ---------------------------------------------------------------------------


def test_unify_var_with_string_binds_name() -> None:
    """mm.var('x') against 'json' returns {'x': 'json'}."""
    assert unify(var("x"), "json") == {"x": "json"}


def test_unify_string_with_var_binds_name_symmetry() -> None:
    """Symmetry: unify('json', mm.var('x')) → {'x': 'json'}."""
    assert unify("json", var("x")) == {"x": "json"}


def test_unify_var_with_struct_binds_struct() -> None:
    """mm.var('t') against a Struct captures the entire struct."""
    some_struct = _s(kind="type", name="string")
    result = unify(var("t"), some_struct)
    assert result == {"t": some_struct}


def test_unify_var_occurrence_check_same_value_idempotent() -> None:
    """Same var bound to same value twice → single binding (idempotent)."""
    initial = {"x": "json"}
    result = unify(var("x"), "json", bindings=initial)
    assert result == {"x": "json"}


def test_unify_var_occurrence_check_conflict_returns_none() -> None:
    """Same var name bound to different values → None (occurrence check conflict)."""
    initial = {"x": "json"}
    result = unify(var("x"), "parquet", bindings=initial)
    assert result is None


def test_unify_var_inherits_existing_bindings() -> None:
    """unify() with existing bindings merges new binding into existing dict."""
    existing = {"y": "parquet"}
    result = unify(var("x"), "json", bindings=existing)
    assert result == {"y": "parquet", "x": "json"}


def test_unify_does_not_mutate_input_bindings() -> None:
    """unify() never mutates the passed-in bindings dict."""
    original = {"x": "json"}
    original_copy = dict(original)
    unify(var("x"), "json", bindings=original)
    assert original == original_copy


# ---------------------------------------------------------------------------
# mm.literal — exact value match (task 4.3)
# ---------------------------------------------------------------------------


def test_unify_literal_matching_string_returns_empty_dict() -> None:
    """mm.literal('json') against 'json' → {}."""
    assert unify(literal("json"), "json") == {}


def test_unify_literal_non_matching_string_returns_none() -> None:
    """mm.literal('json') against 'parquet' → None."""
    assert unify(literal("json"), "parquet") is None


def test_unify_literal_symmetry() -> None:
    """Symmetry: unify('json', mm.literal('json')) → {}."""
    assert unify("json", literal("json")) == {}


def test_unify_literal_symmetry_mismatch() -> None:
    """Symmetry: unify('parquet', mm.literal('json')) → None."""
    assert unify("parquet", literal("json")) is None


def test_unify_literal_integer_match() -> None:
    """mm.literal(42) against 42 → {}."""
    assert unify(literal(42), 42) == {}


def test_unify_literal_integer_mismatch() -> None:
    """mm.literal(42) against 99 → None."""
    assert unify(literal(42), 99) is None


def test_unify_literal_none_value_match() -> None:
    """mm.literal(None) matches None."""
    assert unify(literal(None), None) == {}


# ---------------------------------------------------------------------------
# mm.or_ — left-to-right first match (task 4.4)
# ---------------------------------------------------------------------------


def test_unify_or_first_branch_matches() -> None:
    """First branch match: returns that branch's bindings."""
    assert unify(or_("json", "parquet"), "json") == {}


def test_unify_or_second_branch_matches_when_first_fails() -> None:
    """Second branch match: returns second branch bindings when first fails."""
    assert unify(or_("json", "parquet"), "parquet") == {}


def test_unify_or_all_branches_fail_returns_none() -> None:
    """All branches fail: mm.or_ returns None."""
    assert unify(or_("json", "parquet"), "csv") is None


def test_unify_or_symmetry() -> None:
    """Symmetry: unify('json', mm.or_('json', 'parquet')) → {}."""
    assert unify("json", or_("json", "parquet")) == {}


def test_unify_or_captures_first_branch_var_binding() -> None:
    """mm.or_ with var branches: first branch match captures its binding."""
    result = unify(or_(var("x"), var("y")), "json")
    assert result == {"x": "json"}


def test_unify_or_captures_second_branch_binding_when_first_fails() -> None:
    """mm.or_ with literal + var: second branch captures binding when first fails."""
    result = unify(or_(literal("parquet"), var("x")), "json")
    assert result == {"x": "json"}


def test_unify_or_single_branch_match() -> None:
    """Single-branch mm.or_ behaves like the inner pattern."""
    assert unify(or_("json"), "json") == {}
    assert unify(or_("json"), "parquet") is None


# ---------------------------------------------------------------------------
# mm.T / mm_scalar_pattern — type name match (task 4.5)
# ---------------------------------------------------------------------------


def test_unify_T_matching_type_name_returns_empty_dict() -> None:
    """mm.T('string') against string type struct → {}."""
    assert unify(T("string"), STRING_TYPE) == {}


def test_unify_T_non_matching_type_name_returns_none() -> None:
    """mm.T('string') against integer type struct → None."""
    assert unify(T("string"), INTEGER_TYPE) is None


def test_unify_T_no_bindings_produced() -> None:
    """mm.T unification produces no bindings (type check only)."""
    result = unify(T("string"), STRING_TYPE)
    assert result == {}


def test_unify_T_uses_name_fallback() -> None:
    """mm.T falls back to arg.name when arg.type_name is absent."""
    arg = _s(kind="type", name="string")
    assert unify(T("string"), arg) == {}


def test_unify_T_against_non_type_struct_returns_none() -> None:
    """mm.T against a struct with wrong kind returns None."""
    arg = _s(kind="representation", name="json")
    assert unify(T("json"), arg) is None


def test_unify_T_symmetry() -> None:
    """Symmetry: unify(string_type, mm.T('string')) → {}."""
    assert unify(STRING_TYPE, T("string")) == {}


# ---------------------------------------------------------------------------
# Repr constants — mm_repr_pattern (task 4.6)
# ---------------------------------------------------------------------------


def test_unify_repr_pattern_matching_returns_empty_dict() -> None:
    """mm.json against json repr struct → {}."""
    json_pattern = repr_pattern("json")
    assert unify(json_pattern, JSON_ARG) == {}


def test_unify_repr_pattern_non_matching_returns_none() -> None:
    """mm.json against parquet repr struct → None."""
    json_pattern = repr_pattern("json")
    assert unify(json_pattern, PARQUET_ARG) is None


def test_unify_repr_pattern_no_bindings_produced() -> None:
    """Repr pattern unification produces no bindings."""
    result = unify(repr_pattern("json"), JSON_ARG)
    assert result == {}


def test_unify_repr_pattern_uses_repr_name_field() -> None:
    """Repr arg with repr_name field (not just name) is matched correctly."""
    arg = _s(kind="representation", repr_name="json")
    assert unify(repr_pattern("json"), arg) == {}


def test_unify_repr_pattern_against_non_repr_struct_returns_none() -> None:
    """Repr pattern against a non-representation struct returns None."""
    arg = _s(kind="type", name="json")
    assert unify(repr_pattern("json"), arg) is None


def test_unify_repr_pattern_symmetry() -> None:
    """Symmetry: unify(json_arg, mm.json) → {}."""
    assert unify(JSON_ARG, repr_pattern("json")) == {}


# ---------------------------------------------------------------------------
# mm_entity_pattern — entity kind/name + field recursion (task 4.7)
# ---------------------------------------------------------------------------


def test_unify_entity_pattern_kind_and_name_match_no_fields() -> None:
    """Entity pattern with no field_patterns: kind+name match → {}."""
    pat = entity_pattern("type", "vector")
    arg = _s(kind="type", type_name="vector", name="vector")
    assert unify(pat, arg) == {}


def test_unify_entity_pattern_kind_mismatch_returns_none() -> None:
    """Entity pattern: wrong kind → None."""
    pat = entity_pattern("type", "vector")
    arg = _s(kind="representation", name="vector")
    assert unify(pat, arg) is None


def test_unify_entity_pattern_name_mismatch_returns_none() -> None:
    """Entity pattern: right kind but wrong name → None."""
    pat = entity_pattern("type", "vector")
    arg = _s(kind="type", type_name="matrix", name="matrix")
    assert unify(pat, arg) is None


def test_unify_entity_pattern_field_var_captures_value() -> None:
    """Field pattern mm.var captures the field value."""
    element_type = _s(kind="type", type_name="string", name="string")
    pat = entity_pattern("type", "vector", {"element_type": var("elem")})
    arg = _s(kind="type", type_name="vector", name="vector", element_type=element_type)
    result = unify(pat, arg)
    assert result == {"elem": element_type}


def test_unify_entity_pattern_field_literal_match() -> None:
    """Field pattern literal matches exact field value."""
    pat = entity_pattern("type", "vector", {"element_type": literal(STRING_TYPE)})
    arg = _s(kind="type", type_name="vector", name="vector", element_type=STRING_TYPE)
    assert unify(pat, arg) == {}


def test_unify_entity_pattern_field_literal_mismatch_returns_none() -> None:
    """Field pattern literal mismatch → None."""
    pat = entity_pattern("type", "vector", {"element_type": literal(INTEGER_TYPE)})
    arg = _s(kind="type", type_name="vector", name="vector", element_type=STRING_TYPE)
    assert unify(pat, arg) is None


def test_unify_entity_pattern_missing_field_matches_none() -> None:
    """Field pattern on absent field: getattr returns None, unify(pattern, None)."""
    pat = entity_pattern("type", "vector", {"element_type": ANY})
    arg = _s(kind="type", type_name="vector", name="vector")  # no element_type
    assert unify(pat, arg) == {}


def test_unify_entity_pattern_uses_name_fallback() -> None:
    """Entity pattern uses arg.name when arg.type_name is absent."""
    pat = entity_pattern("type", "vector")
    arg = _s(kind="type", name="vector")  # no type_name
    assert unify(pat, arg) == {}


# ---------------------------------------------------------------------------
# Binding merge / occurrence check (task 4.8)
# ---------------------------------------------------------------------------


def test_unify_two_vars_in_entity_both_bound() -> None:
    """Two different vars in a composite pattern → both in returned dict."""
    element_type = _s(kind="type", type_name="string", name="string")
    pat = entity_pattern(
        "type",
        "vector",
        {
            "element_type": var("elem"),
            "name": var("nm"),
        },
    )
    arg = _s(kind="type", type_name="vector", name="vector", element_type=element_type)
    result = unify(pat, arg)
    assert result is not None
    assert result["elem"] == element_type
    assert result["nm"] == "vector"


def test_unify_same_var_two_positions_same_value_succeeds() -> None:
    """Same var name at two field positions, same value → single binding."""
    pat = entity_pattern(
        "type",
        "vector",
        {
            "type_name": var("n"),
            "name": var("n"),
        },
    )
    # type_name == name == "vector", so binding is consistent
    arg = _s(kind="type", type_name="vector", name="vector")
    result = unify(pat, arg)
    assert result == {"n": "vector"}


def test_unify_same_var_two_positions_different_values_returns_none() -> None:
    """Same var name at two positions with different values → None."""
    pat = entity_pattern(
        "type",
        "pair",
        {
            "type_name": var("n"),
            "name": var("n"),
        },
    )
    # type_name == "pair" but name == "different"
    arg = _s(kind="type", type_name="pair", name="different")
    assert unify(pat, arg) is None


# ---------------------------------------------------------------------------
# Recursive descent (task 4.7 + 4.8)
# ---------------------------------------------------------------------------


def test_unify_nested_entity_pattern_extracts_deep_binding() -> None:
    """Nested entity pattern: var in element_type of a vector → binding extracted."""
    string_type = _s(kind="type", type_name="string", name="string")
    vector_arg = _s(kind="type", type_name="vector", name="vector", element_type=string_type)

    inner_pat = entity_pattern("type", "string")
    outer_pat = entity_pattern("type", "vector", {"element_type": inner_pat})

    assert unify(outer_pat, vector_arg) == {}


def test_unify_nested_entity_var_captured() -> None:
    """Three-level nesting: var at inner level is still captured."""
    inner_arg = _s(kind="type", type_name="string", name="string")
    mid_arg = _s(kind="type", type_name="vector", name="vector", element_type=inner_arg)

    inner_pat = entity_pattern("type", "string")
    mid_pat = entity_pattern("type", "vector", {"element_type": inner_pat})
    outer_pat = entity_pattern("type", "wrapper", {"inner": mid_pat})
    outer_arg = _s(kind="type", type_name="wrapper", name="wrapper", inner=mid_arg)

    result = unify(outer_pat, outer_arg)
    assert result == {}


def test_unify_nested_entity_var_three_levels_deep() -> None:
    """Three levels of entity nesting with a var at the innermost level."""
    leaf = _s(kind="type", type_name="string", name="string")
    mid = _s(kind="type", type_name="vector", name="vector", element_type=leaf)
    top = _s(kind="type", type_name="wrapper", name="wrapper", inner=mid)

    inner_pat = entity_pattern("type", "string")
    mid_pat = entity_pattern("type", "vector", {"element_type": var("elem")})
    top_pat = entity_pattern("type", "wrapper", {"inner": mid_pat})

    result = unify(top_pat, top)
    assert result == {"elem": leaf}


# ---------------------------------------------------------------------------
# bindings=None default (task 4.1)
# ---------------------------------------------------------------------------


def test_unify_none_bindings_defaults_to_empty_dict() -> None:
    """bindings=None (default) behaves like bindings={}."""
    assert unify(var("x"), "json") == {"x": "json"}
    assert unify(var("x"), "json", bindings=None) == {"x": "json"}
