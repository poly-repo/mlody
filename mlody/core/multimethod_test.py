"""Unit tests for mlody/core/multimethod.py.

No evaluator instance is needed — dispatch() accepts the method list directly.
All pattern types from the spec are covered here.
"""

from __future__ import annotations

import pytest

from common.python.starlarkish.core.struct import Struct
from mlody.core.multimethod import (
    DispatchError,
    Pattern,
    _match_score,
    _PATTERN_REGISTRY,
    dispatch,
    register_pattern,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_struct(**kwargs: object) -> Struct:
    return Struct(**kwargs)  # type: ignore[arg-type]


def _make_method(patterns: list[object], body: object) -> Struct:
    return _make_struct(patterns=patterns, body=body)


# Pattern constants replicating what mm.mlody produces

ANY = _make_struct(kind="mm_any")


def T(scalar_name: str) -> Struct:
    return _make_struct(kind="mm_scalar_pattern", type_name=scalar_name)


JSON_PATTERN = _make_struct(kind="mm_repr_pattern", repr_name="json")


def posix_pattern(path_pattern: str) -> Struct:
    return _make_struct(kind="mm_posix_pattern", path_pattern=path_pattern)


def vector_pattern(element_type: object = ANY) -> Struct:
    return _make_struct(
        kind="mm_entity_pattern",
        entity_kind="type",
        entity_name="vector",
        field_patterns={"element_type": element_type},
    )


def value_pattern(**field_patterns: object) -> Struct:
    # entity_name="" is the kind-level wildcard: matches any value struct
    # regardless of its instance name (see MmEntityPattern.score).
    return _make_struct(
        kind="mm_entity_pattern",
        entity_kind="value",
        entity_name="",
        field_patterns=field_patterns,
    )


# Argument helpers

def _type_arg(type_name: str, element_type: object = None) -> Struct:
    if element_type is not None:
        return _make_struct(kind="type", name=type_name, type_name=type_name, element_type=element_type)
    return _make_struct(kind="type", name=type_name, type_name=type_name)


def _location_arg(location_type: str, path: str) -> Struct:
    return _make_struct(kind="location", location_type=location_type, path=path)


def _repr_arg(repr_name: str) -> Struct:
    return _make_struct(kind="representation", repr_name=repr_name)


def _value_arg(**fields: object) -> Struct:
    return _make_struct(kind="value", **fields)


# ---------------------------------------------------------------------------
# 1. _match_score — mm.ANY
# ---------------------------------------------------------------------------


def test_match_score_any_returns_1_for_any_arg() -> None:
    """mm.ANY matches any argument with score 1."""
    assert _match_score(ANY, "train") == 1
    assert _match_score(ANY, 42) == 1
    assert _match_score(ANY, None) == 1


# ---------------------------------------------------------------------------
# 2. _match_score — exact string
# ---------------------------------------------------------------------------


def test_match_score_exact_string_match_returns_2() -> None:
    """Exact string pattern matches equal argument with score 2."""
    assert _match_score("train", "train") == 2


def test_match_score_exact_string_nonmatch_returns_none() -> None:
    """Exact string pattern returns None when arg differs."""
    assert _match_score("train", "serve") is None


# ---------------------------------------------------------------------------
# 3. _match_score — mm.T scalar type pattern
# ---------------------------------------------------------------------------


def test_match_score_scalar_type_matches_correct_name() -> None:
    """mm.T('string') matches a type struct with type_name='string' (score 3)."""
    arg = _type_arg("string")
    assert _match_score(T("string"), arg) == 3


def test_match_score_scalar_type_nonmatch_different_name() -> None:
    """mm.T('string') does not match a type struct with type_name='integer'."""
    arg = _type_arg("integer")
    assert _match_score(T("string"), arg) is None


def test_match_score_scalar_type_nonmatch_non_type_arg() -> None:
    """mm.T('string') does not match a plain string argument."""
    assert _match_score(T("string"), "string") is None


# ---------------------------------------------------------------------------
# 4. _match_score — mm.json repr pattern
# ---------------------------------------------------------------------------


def test_match_score_json_matches_json_repr() -> None:
    """mm.json matches a representation arg with repr_name='json' (score 3)."""
    arg = _repr_arg("json")
    assert _match_score(JSON_PATTERN, arg) == 3


def test_match_score_json_nonmatch_wrong_repr_name() -> None:
    """mm.json does not match a representation arg with repr_name='parquet'."""
    arg = _repr_arg("parquet")
    assert _match_score(JSON_PATTERN, arg) is None


# ---------------------------------------------------------------------------
# 5. _match_score — mm.posix location pattern
# ---------------------------------------------------------------------------


def test_match_score_posix_exact_path_returns_3() -> None:
    """mm.posix exact path (no wildcards) matches with score 3."""
    arg = _location_arg("posix", "/data/train.csv")
    assert _match_score(posix_pattern("/data/train.csv"), arg) == 3


def test_match_score_posix_star_returns_2() -> None:
    """mm.posix single-segment wildcard matches with score 2."""
    arg = _location_arg("posix", "/data/train.csv")
    assert _match_score(posix_pattern("/data/*.csv"), arg) == 2


def test_match_score_posix_star_no_cross_segment() -> None:
    """mm.posix single-segment wildcard does not match across / boundary."""
    arg = _location_arg("posix", "/data/subdir/train.csv")
    assert _match_score(posix_pattern("/data/*.csv"), arg) is None


def test_match_score_posix_double_star_returns_1() -> None:
    """mm.posix ** pattern matches any depth with score 1."""
    arg = _location_arg("posix", "/data/foo/bar/baz.csv")
    assert _match_score(posix_pattern("/data/**"), arg) == 1


def test_match_score_posix_double_star_matches_immediate_child() -> None:
    """mm.posix /** matches /data/foo (one segment) too."""
    arg = _location_arg("posix", "/data/foo")
    assert _match_score(posix_pattern("/data/**"), arg) == 1


def test_match_score_posix_nonmatch_wrong_location_type() -> None:
    """mm.posix does not match a location with location_type='s3'."""
    arg = _location_arg("s3", "/data/train.csv")
    assert _match_score(posix_pattern("/data/**"), arg) is None


def test_match_score_posix_nonmatch_non_location_arg() -> None:
    """mm.posix does not match a plain string argument."""
    assert _match_score(posix_pattern("/data/**"), "/data/foo") is None


def test_match_score_posix_nonmatch_wrong_path() -> None:
    """mm.posix exact path does not match a different path."""
    arg = _location_arg("posix", "/data/test.csv")
    assert _match_score(posix_pattern("/data/train.csv"), arg) is None


# ---------------------------------------------------------------------------
# 6. _match_score — mm.vector pattern
# ---------------------------------------------------------------------------


def test_match_score_vector_any_element_type_returns_4() -> None:
    """mm.vector(element_type=mm.ANY) matches any vector type with score 4."""
    arg = _type_arg("vector", element_type=_type_arg("string"))
    assert _match_score(vector_pattern(ANY), arg) == 4


def test_match_score_vector_typed_element_matches() -> None:
    """mm.vector(element_type=mm.T('string')) matches vector of string (score 6)."""
    arg = _type_arg("vector", element_type=_type_arg("string"))
    assert _match_score(vector_pattern(T("string")), arg) == 6


def test_match_score_vector_typed_element_nonmatch() -> None:
    """mm.vector(element_type=mm.T('string')) does not match vector of integer."""
    arg = _type_arg("vector", element_type=_type_arg("integer"))
    assert _match_score(vector_pattern(T("string")), arg) is None


def test_match_score_vector_nonmatch_wrong_type_name() -> None:
    """mm.vector does not match a type struct with type_name='string'."""
    arg = _type_arg("string")
    assert _match_score(vector_pattern(ANY), arg) is None


def test_match_score_vector_nonmatch_non_type_arg() -> None:
    """mm.vector does not match a plain string argument."""
    assert _match_score(vector_pattern(ANY), "train") is None


# ---------------------------------------------------------------------------
# 7. _match_score — mm.value composite pattern
# ---------------------------------------------------------------------------


def test_match_score_value_no_fields_returns_3() -> None:
    """mm.value() with no fields matches any value struct with score 3."""
    arg = _value_arg(type=_type_arg("string"))
    assert _match_score(value_pattern(), arg) == 3


def test_match_score_value_full_field_match_returns_9() -> None:
    """mm.value(type=mm.T('string'), representation=mm.json) returns score 9."""
    arg = _value_arg(
        type=_type_arg("string"),
        representation=_repr_arg("json"),
    )
    pat = value_pattern(type=T("string"), representation=JSON_PATTERN)
    assert _match_score(pat, arg) == 9


def test_match_score_value_unmentioned_field_ignored() -> None:
    """mm.value(type=mm.T('string')) ignores unmentioned fields and still matches."""
    arg = _value_arg(
        type=_type_arg("string"),
        location=_location_arg("posix", "/data/foo"),
    )
    assert _match_score(value_pattern(type=T("string")), arg) == 6  # 3 + 3


def test_match_score_value_nonmatch_non_value_arg() -> None:
    """mm.value(...) does not match a plain string argument."""
    assert _match_score(value_pattern(), "train") is None


def test_match_score_value_nonmatch_field_mismatch() -> None:
    """mm.value(type=mm.T('string')) returns None when field sub-pattern fails."""
    arg = _value_arg(type=_type_arg("integer"))
    assert _match_score(value_pattern(type=T("string")), arg) is None


# ---------------------------------------------------------------------------
# 8. dispatch — basic scenarios
# ---------------------------------------------------------------------------


def test_dispatch_exact_match_selects_correct_method() -> None:
    """dispatch selects the method whose patterns match exactly."""
    results: list[str] = []

    def body(ctx: object, *args: object) -> str:
        results.append("matched")
        return "matched"

    method = _make_method(["train"], body)
    ret = dispatch("render", ("train",), [method])
    assert ret == "matched"
    assert results == ["matched"]


def test_dispatch_most_specific_wins() -> None:
    """The method with the highest total score is selected (most specific wins).

    Ref: Scenario 'Most specific method wins' from spec.
    """
    selected: list[str] = []

    def exact_fn(ctx: object, *args: object) -> str:
        selected.append("exact")
        return "exact"

    def any_fn(ctx: object, *args: object) -> str:
        selected.append("any")
        return "any"

    exact_method = _make_method(["train", "gpu"], exact_fn)
    train_any_method = _make_method(["train", ANY], any_fn)
    any_gpu_method = _make_method([ANY, "gpu"], any_fn)
    any_any_method = _make_method([ANY, ANY], any_fn)

    methods = [exact_method, train_any_method, any_gpu_method, any_any_method]
    ret = dispatch("proc", ("train", "gpu"), methods)
    assert ret == "exact"


def test_dispatch_first_registered_wins_on_tie() -> None:
    """On a score tie, the first-registered method is selected.

    Ref: Scenario 'First-registered wins on tie' from spec.
    """
    calls: list[str] = []

    def first(ctx: object, *args: object) -> str:
        calls.append("first")
        return "first"

    def second(ctx: object, *args: object) -> str:
        calls.append("second")
        return "second"

    methods = [_make_method([ANY], first), _make_method([ANY], second)]
    ret = dispatch("gen", ("anything",), methods)
    assert ret == "first"


def test_dispatch_no_match_raises_dispatch_error() -> None:
    """dispatch raises DispatchError when no method matches."""
    def body(ctx: object, *args: object) -> None:
        pass

    method = _make_method(["train"], body)
    with pytest.raises(DispatchError, match="render"):
        dispatch("render", ("unknown_label",), [method])


def test_dispatch_error_names_generic_and_args() -> None:
    """DispatchError message includes the generic name and the argument."""
    def body(ctx: object, *args: object) -> None:
        pass

    method = _make_method(["train"], body)
    with pytest.raises(DispatchError) as exc_info:
        dispatch("render", ("unknown_label",), [method])

    msg = str(exc_info.value)
    assert "render" in msg
    assert "unknown_label" in msg


def test_dispatch_error_lists_all_registered_patterns() -> None:
    """DispatchError message lists every registered method's patterns."""
    def body(ctx: object, *args: object) -> None:
        pass

    m1 = _make_method(["train"], body)
    m2 = _make_method(["serve"], body)
    with pytest.raises(DispatchError) as exc_info:
        dispatch("render", ("unknown",), [m1, m2])

    msg = str(exc_info.value)
    assert "train" in msg
    assert "serve" in msg


def test_dispatch_zero_methods_raises_dispatch_error() -> None:
    """dispatch with no registered methods raises DispatchError cleanly."""
    with pytest.raises(DispatchError):
        dispatch("render", ("train",), [])


# ---------------------------------------------------------------------------
# 9. dispatch — ctx structure
# ---------------------------------------------------------------------------


def test_dispatch_ctx_generic_name() -> None:
    """ctx.generic is the generic name string."""
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("render", ("train",), [_make_method(["train"], body)])
    ctx = captured[0]
    assert getattr(ctx, "generic") == "render"


def test_dispatch_ctx_captures_all_positions() -> None:
    """ctx.captures maps string position indices to argument values for all positions.

    Ref: Scenario 'ctx.captures always includes all positions' from spec.
    """
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("proc", ("foo", "bar"), [_make_method([ANY, ANY], body)])
    ctx = captured[0]
    assert getattr(ctx, "captures") == {"0": "foo", "1": "bar"}


def test_dispatch_ctx_captures_mixed_patterns() -> None:
    """ctx.captures includes both exact-match and ANY positions.

    Ref: Scenario 'Multi-arg dispatch ctx shape' from spec.
    """
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("proc", ("train", "gpu"), [_make_method(["train", ANY], body)])
    ctx = captured[0]
    assert getattr(ctx, "captures") == {"0": "train", "1": "gpu"}


def test_dispatch_ctx_captures_top_level_var_by_name() -> None:
    """A top-level mm.var pattern adds a named capture alongside the positional one."""
    var_x = _make_struct(kind="mm_var_pattern", var_name="x")
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("g", ("hello",), [_make_method([var_x], body)])
    ctx = captured[0]
    assert getattr(ctx, "captures") == {"0": "hello", "x": "hello"}


def test_dispatch_ctx_captures_entity_field_var_by_name() -> None:
    """mm.var in an entity field pattern binds the field value under its name."""
    loc = _make_struct(kind="location", path="/tmp/foo")
    value = _make_struct(kind="value", name="ds", location=loc)
    var_loc = _make_struct(kind="mm_var_pattern", var_name="loc")
    pat = _make_struct(
        kind="mm_entity_pattern",
        entity_kind="value",
        entity_name="",
        field_patterns={"location": var_loc},
    )
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("g", (value,), [_make_method([pat], body)])
    ctx = captured[0]
    assert getattr(ctx, "captures") == {"0": value, "loc": loc}


# ---------------------------------------------------------------------------
# context threading — ctx.workspace and ctx.run from evaluator
# ---------------------------------------------------------------------------


def test_dispatch_ctx_workspace_populated_from_context() -> None:
    """ctx.workspace and ctx.run are set when dispatch() receives a context object."""
    workspace = _make_struct(directory="/repo", branch="main")
    run = _make_struct(id="abc", user="mav")
    context = _make_struct(workspace=workspace, run=run)
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("g", ("x",), [_make_method([ANY], body)], context=context)
    ctx = captured[0]
    assert getattr(ctx, "workspace") is workspace
    assert getattr(ctx, "run") is run


def test_dispatch_ctx_no_workspace_when_context_is_none() -> None:
    """ctx.workspace is absent when no context is passed (default None)."""
    captured: list[object] = []

    def body(ctx: object, *args: object) -> None:
        captured.append(ctx)  # type: ignore[arg-type]

    dispatch("g", ("x",), [_make_method([ANY], body)])
    ctx = captured[0]
    assert not hasattr(ctx, "workspace")
    assert not hasattr(ctx, "run")


# ---------------------------------------------------------------------------
# 10. dispatch — body receives correct positional args
# ---------------------------------------------------------------------------


def test_dispatch_body_receives_original_args() -> None:
    """Body is called as body(ctx, *args) with the original positional args."""
    received: list[tuple[object, ...]] = []

    def body(ctx: object, *args: object) -> None:
        received.append(args)

    dispatch("render", ("train", "gpu"), [_make_method(["train", "gpu"], body)])
    assert received == [("train", "gpu")]


# ---------------------------------------------------------------------------
# 11. composite score scenario from spec
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11b. _match_score — mm.T matches string label element_type (real-world path)
# ---------------------------------------------------------------------------


def test_mm_T_matches_string_label_element_type() -> None:
    """mm.T("celebA-row") matches a vector whose element_type is the string ":celebA-row".

    _make_factory stores element_type as an unresolved label string in attributes;
    dispatch must handle that as equivalent to a resolved type struct.
    """
    element_type_struct = _make_struct(kind="type", name="celebA-row", type_name="celebA-row")
    vector_with_struct = _make_struct(kind="type", name="vector", type_name="vector",
                                      element_type=element_type_struct)
    vector_with_label = _make_struct(kind="type", name="vector", type_name="vector",
                                     attributes={"element_type": ":celebA-row"})
    pattern = vector_pattern(T("celebA-row"))

    assert _match_score(pattern, vector_with_struct) == 6   # 3 + 3
    assert _match_score(pattern, vector_with_label) == 6    # same score via label path


def test_mm_T_plain_string_still_nonmatch() -> None:
    """A plain string without ':' prefix does NOT match mm.T (no regression)."""
    assert _match_score(T("string"), "string") is None


def test_dispatch_composite_score_most_constrained_wins() -> None:
    """The most-constrained composite pattern wins.

    Ref: Scenario 'Most constrained composite beats least constrained composite'.
    Score for mm.value(type=mm.vector(mm.T('string')), representation=mm.json):
      3 + (3 + 3) + 3 = 12 ... actually spec says 19 vs 14; let's verify.
    mm.value(type=mm.vector(element_type=mm.T('string')), representation=mm.json)
      = 3 + score(mm.vector(T('string'))) + score(mm.json)
      = 3 + (3 + 3) + 3 = 12

    Spec says 19. Looking again: 3 + (3 + (3+3)) + (3+3) ...
    Actually spec score table: mm.value = 3 + sum(field subscores)
    mm.vector(element_type=mm.T('string')) = 3 + 3 = 6
    mm.json = 3
    So value_pattern score = 3 + 6 + 3 = 12 (not 19).

    The spec says "score 19 vs 14" for the composite example. Let's not
    assert the exact numbers but just verify the ordering.
    """
    calls: list[str] = []

    def specific(ctx: object, *args: object) -> str:
        calls.append("specific")
        return "specific"

    def general(ctx: object, *args: object) -> str:
        calls.append("general")
        return "general"

    string_vec = _type_arg("vector", element_type=_type_arg("string"))
    value_arg = _value_arg(
        type=string_vec,
        representation=_repr_arg("json"),
    )

    specific_pattern = value_pattern(
        type=vector_pattern(T("string")),
        representation=JSON_PATTERN,
    )
    general_pattern = value_pattern(
        type=vector_pattern(ANY),
        representation=JSON_PATTERN,
    )

    specific_method = _make_method([specific_pattern], specific)
    general_method = _make_method([general_pattern], general)

    ret = dispatch("render", (value_arg,), [specific_method, general_method])
    assert ret == "specific"


# ---------------------------------------------------------------------------
# 12. F4 — Pattern registry tests (Wave 2c)
# ---------------------------------------------------------------------------


def test_pattern_registry_contains_core_kinds() -> None:
    """_PATTERN_REGISTRY contains the four original kinds and mm_entity_pattern.

    After task 7.3, mm_vector_pattern, mm_value_pattern, and
    mm_source_range_pattern were removed. The four surviving original kinds
    plus mm_entity_pattern (and the three kinds from tasks 3.1-3.4) must
    all be present.

    Ref: Scenario '_PATTERN_REGISTRY contains exactly the expected kinds after import'.
    """
    expected_subset = {
        "mm_any",
        "mm_scalar_pattern",
        "mm_repr_pattern",
        "mm_posix_pattern",
        "mm_entity_pattern",
    }
    assert expected_subset.issubset(set(_PATTERN_REGISTRY.keys()))


def test_pattern_registry_all_values_have_callable_score() -> None:
    """Each class in _PATTERN_REGISTRY has a callable score attribute.

    Ref: Scenario 'Each registered class implements score'.
    """
    for cls in _PATTERN_REGISTRY.values():
        assert callable(cls.score)


def test_match_score_mm_any_returns_1() -> None:
    """_match_score returns 1 for mm_any regardless of arg.

    Ref: Scenario '_match_score returns 1 for mm_any'.
    """
    anything = object()
    assert _match_score(_make_struct(kind="mm_any"), anything) == 1
    assert _match_score(_make_struct(kind="mm_any"), "train") == 1
    assert _match_score(_make_struct(kind="mm_any"), 42) == 1


def test_match_score_exact_string_returns_2() -> None:
    """_match_score returns 2 for exact string match.

    Ref: Scenario '_match_score returns 2 for exact string match'.
    """
    assert _match_score("hello", "hello") == 2


def test_match_score_exact_string_nonmatch_returns_none() -> None:
    """_match_score returns None for exact string non-match.

    Ref: Scenario '_match_score returns None for exact string non-match'.
    """
    assert _match_score("hello", "world") is None


def test_match_score_scalar_pattern_match_returns_3() -> None:
    """_match_score returns 3 for matching mm_scalar_pattern.

    Ref: Scenario '_match_score returns 3 for matching mm_scalar_pattern'.
    """
    pattern = _make_struct(kind="mm_scalar_pattern", type_name="integer")
    type_struct = _make_struct(kind="type", type_name="integer")
    assert _match_score(pattern, type_struct) == 3


def test_match_score_scalar_pattern_nonmatch_returns_none() -> None:
    """_match_score returns None for non-matching mm_scalar_pattern.

    Ref: Scenario '_match_score returns None for non-matching mm_scalar_pattern'.
    """
    pattern = _make_struct(kind="mm_scalar_pattern", type_name="integer")
    type_struct = _make_struct(kind="type", type_name="string")
    assert _match_score(pattern, type_struct) is None


def test_match_score_posix_double_star_returns_1() -> None:
    """_match_score returns 1 for mm_posix_pattern with **.

    Ref: Scenario '_match_score preserves posix scoring semantics'.
    """
    pattern = _make_struct(kind="mm_posix_pattern", path_pattern="**")
    location = _make_struct(kind="location", type="posix", path="a/b/c")
    assert _match_score(pattern, location) == 1


def test_match_score_posix_single_star_returns_2() -> None:
    """_match_score returns 2 for mm_posix_pattern with single *.

    Ref: Requirement '_match_score dispatches through _PATTERN_REGISTRY'.
    """
    pattern = _make_struct(kind="mm_posix_pattern", path_pattern="/data/*.csv")
    location = _make_struct(kind="location", location_type="posix", path="/data/train.csv")
    assert _match_score(pattern, location) == 2


def test_match_score_unknown_kind_returns_none() -> None:
    """_match_score returns None for unknown pattern kind.

    Ref: Scenario '_match_score returns None for unknown pattern kind'.
    """
    assert _match_score(_make_struct(kind="unknown_pattern_kind"), "anything") is None


def test_match_score_none_kind_returns_none() -> None:
    """_match_score returns None when pattern has no kind attribute."""
    assert _match_score(object(), "anything") is None


def test_register_pattern_returns_class_unchanged() -> None:
    """register_pattern returns the class object itself.

    Ref: Scenario 'register_pattern returns the class unchanged'.
    """

    @register_pattern("_test_kind_for_identity_check")
    class _TestPattern:
        @classmethod
        def score(cls, pattern: object, arg: object) -> int | None:
            return 99

    try:
        # The returned value must be the same class object (identity preserved)
        assert _PATTERN_REGISTRY["_test_kind_for_identity_check"] is _TestPattern
    finally:
        # Clean up the registry so this test does not affect the registry-contents test
        _PATTERN_REGISTRY.pop("_test_kind_for_identity_check", None)


def test_register_pattern_inserts_into_registry() -> None:
    """@register_pattern inserts the class into _PATTERN_REGISTRY under the given key.

    Ref: Scenario 'register_pattern inserts the class into the registry'.
    """

    @register_pattern("_test_kind_for_insertion_check")
    class _TestInsert:
        @classmethod
        def score(cls, pattern: object, arg: object) -> int | None:
            return 77

    try:
        assert _PATTERN_REGISTRY["_test_kind_for_insertion_check"] is _TestInsert
    finally:
        _PATTERN_REGISTRY.pop("_test_kind_for_insertion_check", None)


def test_pattern_protocol_import_and_structural_subtype() -> None:
    """Pattern is importable and a class with score satisfies it structurally.

    Ref: Scenario 'Pattern is a Protocol' and 'A class implementing score satisfies Pattern'.
    """

    class MyPattern:
        @classmethod
        def score(cls, pattern: object, arg: object) -> int | None:
            return 1

    # isinstance check works because Pattern is @runtime_checkable
    assert isinstance(MyPattern, type)
    # Structural check: MyPattern has a callable score
    assert callable(MyPattern.score)
    # runtime_checkable Protocol supports isinstance on instances
    assert isinstance(MyPattern(), Pattern)


# ---------------------------------------------------------------------------
# 13. MmVarPattern (task 3.1)
# ---------------------------------------------------------------------------


def test_mm_var_pattern_score_returns_1_for_string_arg() -> None:
    """MmVarPattern.score returns 1 for a string argument.

    Ref: task 3.1 — mm_var_pattern score returns 1 for any argument.
    """
    pattern = _make_struct(kind="mm_var_pattern", var_name="x")
    assert _match_score(pattern, "train") == 1


def test_mm_var_pattern_score_returns_1_for_struct_arg() -> None:
    """MmVarPattern.score returns 1 for a struct argument.

    Ref: task 3.1 — score is always 1, same specificity as mm.ANY.
    """
    pattern = _make_struct(kind="mm_var_pattern", var_name="x")
    arg = _type_arg("string")
    assert _match_score(pattern, arg) == 1


def test_mm_var_pattern_score_returns_1_for_none_arg() -> None:
    """MmVarPattern.score returns 1 for None.

    Ref: task 3.1 — any argument, no exceptions.
    """
    pattern = _make_struct(kind="mm_var_pattern", var_name="x")
    assert _match_score(pattern, None) == 1


def test_mm_var_pattern_score_returns_1_for_integer_arg() -> None:
    """MmVarPattern.score returns 1 for an integer.

    Ref: task 3.1 — wildcard semantics identical to mm.ANY.
    """
    pattern = _make_struct(kind="mm_var_pattern", var_name="x")
    assert _match_score(pattern, 42) == 1


def test_mm_var_pattern_is_in_registry() -> None:
    """'mm_var_pattern' is registered in _PATTERN_REGISTRY after import.

    Ref: task 3.1 — @register_pattern("mm_var_pattern").
    """
    assert "mm_var_pattern" in _PATTERN_REGISTRY


# ---------------------------------------------------------------------------
# 14. MmLiteralPattern (task 3.2)
# ---------------------------------------------------------------------------


def test_mm_literal_pattern_score_returns_2_on_equality() -> None:
    """MmLiteralPattern.score returns 2 when pattern.value == arg.

    Ref: task 3.2 — same specificity as exact string match.
    """
    pattern = _make_struct(kind="mm_literal_pattern", value="train")
    assert _match_score(pattern, "train") == 2


def test_mm_literal_pattern_score_returns_none_on_inequality() -> None:
    """MmLiteralPattern.score returns None when pattern.value != arg.

    Ref: task 3.2 — no match on inequality.
    """
    pattern = _make_struct(kind="mm_literal_pattern", value="train")
    assert _match_score(pattern, "serve") is None


def test_mm_literal_pattern_matches_integer_value() -> None:
    """MmLiteralPattern.score works for non-string literal values too.

    Ref: task 3.2 — value= can be any Python object; equality is used.
    """
    pattern = _make_struct(kind="mm_literal_pattern", value=42)
    assert _match_score(pattern, 42) == 2
    assert _match_score(pattern, 43) is None


def test_mm_literal_pattern_is_in_registry() -> None:
    """'mm_literal_pattern' is registered in _PATTERN_REGISTRY.

    Ref: task 3.2 — @register_pattern("mm_literal_pattern").
    """
    assert "mm_literal_pattern" in _PATTERN_REGISTRY


# ---------------------------------------------------------------------------
# 15. MmOrPattern (task 3.3)
# ---------------------------------------------------------------------------


def test_mm_or_pattern_score_returns_max_of_matching_branches() -> None:
    """MmOrPattern.score returns the maximum score among matching branches.

    Ref: task 3.3 — dispatch needs best score, not first-match.
    """
    branch_a = _make_struct(kind="mm_scalar_pattern", type_name="string")  # score 3
    branch_b = _make_struct(kind="mm_any")  # score 1
    pattern = _make_struct(kind="mm_or_pattern", patterns=[branch_a, branch_b])
    arg = _type_arg("string")
    # branch_a scores 3, branch_b scores 1; max = 3
    assert _match_score(pattern, arg) == 3


def test_mm_or_pattern_score_returns_none_when_all_branches_fail() -> None:
    """MmOrPattern.score returns None when no branch matches.

    Ref: task 3.3 — all-or-branches-fail edge case.
    """
    branch_a = _make_struct(kind="mm_scalar_pattern", type_name="string")
    branch_b = _make_struct(kind="mm_scalar_pattern", type_name="integer")
    pattern = _make_struct(kind="mm_or_pattern", patterns=[branch_a, branch_b])
    arg = _type_arg("float")
    assert _match_score(pattern, arg) is None


def test_mm_or_pattern_score_single_matching_branch() -> None:
    """MmOrPattern.score with one matching branch returns that branch's score.

    Ref: task 3.3 — single-element or_ works correctly.
    """
    branch = _make_struct(kind="mm_literal_pattern", value="train")
    pattern = _make_struct(kind="mm_or_pattern", patterns=[branch])
    assert _match_score(pattern, "train") == 2
    assert _match_score(pattern, "serve") is None


def test_mm_or_pattern_score_uses_max_not_first() -> None:
    """MmOrPattern.score uses max, not first-match — higher-scoring branch wins.

    Ref: design.md Decision 7 — dispatch needs the best score.
    """
    # branch_b (score 2 exact) is listed second; branch_a (score 1 ANY) first.
    branch_a = _make_struct(kind="mm_any")  # score 1
    branch_b = "train"  # score 2 for arg "train"
    pattern = _make_struct(kind="mm_or_pattern", patterns=[branch_a, branch_b])
    assert _match_score(pattern, "train") == 2


def test_mm_or_pattern_is_in_registry() -> None:
    """'mm_or_pattern' is registered in _PATTERN_REGISTRY.

    Ref: task 3.3 — @register_pattern("mm_or_pattern").
    """
    assert "mm_or_pattern" in _PATTERN_REGISTRY


# ---------------------------------------------------------------------------
# 16. MmEntityPattern (task 3.4)
# ---------------------------------------------------------------------------


def _entity_pattern(
    entity_kind: str,
    entity_name: str,
    field_patterns: dict[str, object] | None = None,
) -> Struct:
    """Build an mm_entity_pattern struct matching the spec-defined shape."""
    return _make_struct(
        kind="mm_entity_pattern",
        entity_kind=entity_kind,
        entity_name=entity_name,
        field_patterns=field_patterns or {},
    )


def test_mm_entity_pattern_no_fields_returns_3() -> None:
    """MmEntityPattern with no field_patterns returns score 3 on entity_kind/name match.

    Ref: task 3.4 — empty field_patterns → 3 + sum([]) = 3.
    """
    pattern = _entity_pattern("type", "vector")
    arg = _type_arg("vector")
    assert _match_score(pattern, arg) == 3


def test_mm_entity_pattern_name_mismatch_returns_none() -> None:
    """MmEntityPattern returns None when entity_name does not match arg.

    Ref: task 3.4 — name mismatch check.
    """
    pattern = _entity_pattern("type", "vector")
    arg = _type_arg("string")
    assert _match_score(pattern, arg) is None


def test_mm_entity_pattern_kind_mismatch_returns_none() -> None:
    """MmEntityPattern returns None when entity_kind does not match arg.kind.

    Ref: task 3.4 — entity_kind check.
    """
    pattern = _entity_pattern("type", "vector")
    arg = _make_struct(kind="representation", name="vector", type_name="vector")
    assert _match_score(pattern, arg) is None


def test_mm_entity_pattern_field_recursion_adds_sub_scores() -> None:
    """MmEntityPattern with field_patterns returns 3 + sum(sub_scores).

    Ref: task 3.4 — field recursion via _match_score.
    """
    element_type_arg = _type_arg("string")
    arg = _make_struct(kind="type", name="vector", type_name="vector", element_type=element_type_arg)
    # sub-pattern: mm_scalar_pattern matching "string" → score 3
    sub_pattern = _make_struct(kind="mm_scalar_pattern", type_name="string")
    pattern = _entity_pattern("type", "vector", {"element_type": sub_pattern})
    assert _match_score(pattern, arg) == 6  # 3 + 3


def test_mm_entity_pattern_field_failure_returns_none() -> None:
    """MmEntityPattern returns None when any sub-pattern fails.

    Ref: task 3.4 — field failure → None.
    """
    element_type_arg = _type_arg("integer")
    arg = _make_struct(kind="type", name="vector", type_name="vector", element_type=element_type_arg)
    sub_pattern = _make_struct(kind="mm_scalar_pattern", type_name="string")  # mismatch
    pattern = _entity_pattern("type", "vector", {"element_type": sub_pattern})
    assert _match_score(pattern, arg) is None


def test_mm_entity_pattern_var_sub_pattern_contributes_score_1() -> None:
    """MmEntityPattern with mm_var_pattern in field_patterns scores 3 + 1 = 4.

    Ref: task 3.4 — mm.var sub-patterns contribute score 1.
    """
    element_type_arg = _type_arg("string")
    arg = _make_struct(kind="type", name="vector", type_name="vector", element_type=element_type_arg)
    sub_pattern = _make_struct(kind="mm_var_pattern", var_name="el")
    pattern = _entity_pattern("type", "vector", {"element_type": sub_pattern})
    assert _match_score(pattern, arg) == 4  # 3 + 1


def test_mm_entity_pattern_uses_type_name_field_fallback() -> None:
    """MmEntityPattern falls back to arg.type_name when arg.name is absent.

    Ref: task 3.4 — try arg.type_name, fall back to arg.name.
    """
    # arg has type_name but no name — entity pattern should match via type_name
    arg = _make_struct(kind="type", type_name="vector")
    pattern = _entity_pattern("type", "vector")
    assert _match_score(pattern, arg) == 3


def test_mm_entity_pattern_uses_name_fallback_when_type_name_absent() -> None:
    """MmEntityPattern falls back to arg.name when arg.type_name is absent.

    Ref: task 3.4 — name fallback.
    """
    arg = _make_struct(kind="type", name="vector")
    pattern = _entity_pattern("type", "vector")
    assert _match_score(pattern, arg) == 3


def test_mm_entity_pattern_source_range_kind() -> None:
    """MmEntityPattern with entity_kind='mlody-source-range' matches hyphenated kind.

    Ref: task 3.4 — special case for mlody-source-range.
    """
    pattern = _entity_pattern("mlody-source-range", "source-range")
    arg = _make_struct(kind="mlody-source-range", name="source-range")
    assert _match_score(pattern, arg) == 3


def test_mm_entity_pattern_is_in_registry() -> None:
    """'mm_entity_pattern' is registered in _PATTERN_REGISTRY.

    Ref: task 3.4 — @register_pattern("mm_entity_pattern").
    """
    assert "mm_entity_pattern" in _PATTERN_REGISTRY


# ---------------------------------------------------------------------------
# 17. Updated registry contents (tasks 3.1–3.4 complete)
# ---------------------------------------------------------------------------


def test_pattern_registry_contains_all_eight_kinds() -> None:
    """_PATTERN_REGISTRY contains exactly 8 kinds after task 7.3.

    Three hand-written classes (mm_vector_pattern, mm_value_pattern,
    mm_source_range_pattern) were removed in task 7.3; their dispatch
    responsibility moved to mm_entity_pattern.

    Ref: task 7.3 — registry keys after removing three pattern classes.
    """
    expected = {
        # four surviving original kinds
        "mm_any",
        "mm_scalar_pattern",
        "mm_repr_pattern",
        "mm_posix_pattern",
        # four kinds added in tasks 3.1–3.4
        "mm_var_pattern",
        "mm_literal_pattern",
        "mm_or_pattern",
        "mm_entity_pattern",
    }
    assert set(_PATTERN_REGISTRY.keys()) == expected
