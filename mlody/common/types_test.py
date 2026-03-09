"""Integration tests for mlody/common/types.mlody.

File contents are read from the real filesystem at module import time (before
any pyfakefs fixture activates), then fed into InMemoryFS for evaluator tests.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

# ---------------------------------------------------------------------------
# Read real .mlody sources at import time (before any filesystem mocking)
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
}


def _eval(extra_mlody: str) -> Evaluator:
    """Evaluate a test script that loads types.mlody, return the evaluator."""
    script = 'load("//mlody/common/types.mlody")\n' + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


# ---------------------------------------------------------------------------
# 1. Renamed field: attributes (not constraints)
# ---------------------------------------------------------------------------


def test_integer_uses_attributes_field() -> None:
    """integer(min=0) struct has .attributes field, not .constraints."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = integer(min=0)
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert hasattr(data, "attributes"), "expected .attributes field"
    assert not hasattr(data, "constraints"), "legacy .constraints must not exist"
    assert data.attributes == {"min": 0}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. _allowed_attrs is a dict (not a set)
# ---------------------------------------------------------------------------


def test_allowed_attrs_is_dict() -> None:
    """integer()._allowed_attrs is a dict mapping attr names to type names."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = integer()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    allowed = data._allowed_attrs  # type: ignore[attr-defined]
    assert isinstance(allowed, dict), f"_allowed_attrs should be dict, got {type(allowed)}"
    assert allowed == {"min": "integer", "max": "integer"}


# ---------------------------------------------------------------------------
# 3. Unknown attribute rejected at factory call time
# ---------------------------------------------------------------------------


def test_integer_rejects_unknown_attr() -> None:
    """integer(unknown=5) raises TypeError naming the bad attribute."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        integer(unknown=5)
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError, match="unknown"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 4. Wrong attribute value type rejected at factory call time
# ---------------------------------------------------------------------------


def test_integer_rejects_wrong_attr_type() -> None:
    """integer(min='bad') raises TypeError because min must be integer."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        integer(min="bad")
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError, match="min"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 5. typedef with attrs produces type whose _allowed_attrs includes new attr
# ---------------------------------------------------------------------------


def test_typedef_with_attrs() -> None:
    """typedef(..., attrs={"step": attr(type="integer")}) adds step to _allowed_attrs."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "stepped_int",
            base = integer(),
            attrs = {"step": attr(type="integer")},
        )
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    assert "stepped_int" in ev._types_by_name
    t = ev._types_by_name["stepped_int"]
    assert "step" in t._allowed_attrs  # type: ignore[attr-defined]
    assert t._allowed_attrs["step"] == "integer"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6. Conflict check: redeclaring an inherited attr raises ValueError
# ---------------------------------------------------------------------------


def test_typedef_attrs_conflict_raises() -> None:
    """Redeclaring 'min' in attrs when base already has it raises ValueError."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "bad_type",
            base = integer(),
            attrs = {"min": attr(type="integer")},
        )
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(ValueError, match="conflict"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 7. Derived type inherits base _allowed_attrs plus new ones
# ---------------------------------------------------------------------------


def test_typedef_attrs_inherit_base() -> None:
    """Derived type's _allowed_attrs contains both base attrs and new attrs."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "bounded_int",
            base = integer(),
            attrs = {"step": attr(type="integer")},
        )
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._types_by_name["bounded_int"]
    allowed = t._allowed_attrs  # type: ignore[attr-defined]
    # Inherited from integer
    assert "min" in allowed
    assert "max" in allowed
    # Newly declared
    assert "step" in allowed
    assert allowed["min"] == "integer"
    assert allowed["step"] == "integer"


# ---------------------------------------------------------------------------
# 8. Tier-2 validation: attr with user-defined typedef type
# ---------------------------------------------------------------------------


def test_tier2_validation() -> None:
    """A typedef's validator correctly validates values via the base type chain."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="pos_int", base=integer())
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    pos_int = ev._types_by_name["pos_int"]

    # Valid integer passes
    assert pos_int.validator(42)  # type: ignore[attr-defined]

    # Non-integer fails with TypeError (Tier-2 dispatches through the validator chain)
    with pytest.raises(TypeError):
        pos_int.validator("oops")  # type: ignore[attr-defined]

    # Bool is not an int in our type system
    with pytest.raises(TypeError):
        pos_int.validator(True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 9. Factory is auto-injected into scope after typedef()
# ---------------------------------------------------------------------------


def test_typedef_factory_injected_in_scope() -> None:
    """typedef(name="age", ...) injects age() callable into the file's scope."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        builtins.register("root", struct(name="r", factory=age))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    factory = ev._roots_by_name["r"].factory  # type: ignore[attr-defined]
    assert callable(factory)


# ---------------------------------------------------------------------------
# 10. Factory with no kwargs returns the type struct itself
# ---------------------------------------------------------------------------


def test_typedef_factory_no_kwargs_returns_type() -> None:
    """age() with no kwargs returns the age type struct (kind='type')."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        builtins.register("root", struct(name="r", t=age()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.kind == "type"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 11. Factory with valid kwargs produces anonymous sub-type with updated attrs
# ---------------------------------------------------------------------------


def test_typedef_factory_with_valid_kwargs() -> None:
    """age(max=17) returns an anonymous type struct with attributes['max'] == 17."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        builtins.register("root", struct(name="r", t=age(max=17)))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.attributes["max"] == 17  # type: ignore[attr-defined]
    assert t.attributes["min"] == 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 12. Factory combined validator: extra constraints are enforced
# ---------------------------------------------------------------------------


def test_typedef_factory_combined_validator() -> None:
    """age(max=17).validator(18) raises even though age itself allows up to 150."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        builtins.register("root", struct(name="r", child_age=age(max=17)))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    child_age = ev._roots_by_name["r"].child_age  # type: ignore[attr-defined]
    # 17 is valid (at the boundary)
    assert child_age.validator(17)  # type: ignore[attr-defined]
    # 18 exceeds the narrowed max=17
    with pytest.raises(ValueError):
        child_age.validator(18)  # type: ignore[attr-defined]
    # Base constraint min=0 still applies
    with pytest.raises(ValueError):
        child_age.validator(-1)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 13. Factory validates kwarg types
# ---------------------------------------------------------------------------


def test_typedef_factory_validates_kwarg_type() -> None:
    """age(max='bad') raises TypeError because max must be integer."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        age(max="bad")
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError, match="max"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 14. Factory rejects unknown kwargs
# ---------------------------------------------------------------------------


def test_typedef_factory_rejects_unknown_kwarg() -> None:
    """age(bogus=1) raises TypeError naming the unexpected attribute."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        age(bogus=1)
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError, match="bogus"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 15. Factory inherits allowed kwargs across typedef chain
# ---------------------------------------------------------------------------


def test_typedef_factory_inherits_chain_attrs() -> None:
    """typedef(B, base=A) → B's factory accepts both A's attrs and B's own attrs."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name="A",
            base=integer(),
            attrs={"step": attr(type="integer", mandatory=False)},
        )
        typedef(
            name="B",
            base=A(),
            attrs={"factor": attr(type="integer", mandatory=False)},
        )
        # B's factory should accept step (from A), factor (from B), min/max (from integer)
        builtins.register("root", struct(name="r", t=B(step=2, factor=3)))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.attributes["step"] == 2  # type: ignore[attr-defined]
    assert t.attributes["factor"] == 3  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 16. attr(type=struct) — type struct passed directly instead of string name
# ---------------------------------------------------------------------------


def test_attr_type_struct_direct() -> None:
    """attr(type=age(max=17)) uses the struct's validator directly (no name lookup)."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="age", base=integer(min=0, max=150))
        typedef(
            name="child",
            base=string(),
            attrs={"birth_age": attr(type=age(max=17))},
        )
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    child = ev._types_by_name["child"]
    assert "birth_age" in child._allowed_attrs  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 17. Factory is load()-importable from another file
# ---------------------------------------------------------------------------


def test_typedef_factory_cross_file_load() -> None:
    """A factory injected in types_file.mlody is load()-importable by another file."""
    files = dict(_BASE_FILES)
    files["types_file.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="score", base=integer(min=0, max=100))
    """)
    files["consumer.mlody"] = dedent("""\
        load("//types_file.mlody", "score")
        builtins.register("root", struct(name="r", t=score(max=50)))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "consumer.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.attributes["max"] == 50  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 18. Two teams, same name — each file's factory is distinct
# ---------------------------------------------------------------------------


def test_typedef_factory_distinct_across_files() -> None:
    """Two files each define 'score'; loading from the correct file gets the right factory."""
    files = dict(_BASE_FILES)
    files["team_a/types.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="score", base=integer(min=0, max=100))
    """)
    files["team_b/types.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="score", base=integer(min=0, max=10))
    """)
    files["consumer.mlody"] = dedent("""\
        load("//team_a/types.mlody", "score")
        builtins.register("root", struct(name="r", t=score()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "consumer.mlody")

    # score from team_a has max=100 in its attributes
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.attributes.get("max") == 100  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 19. string() factory via typedef works — pattern attr
# ---------------------------------------------------------------------------


def test_string_factory_via_typedef() -> None:
    """string(pattern='[a-z]+') returns a configured type struct."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = string(pattern="[a-z]+")
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.attributes.get("pattern") == "[a-z]+"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 20. float() factory enforces min/max constraints
# ---------------------------------------------------------------------------


def test_float_factory_enforces_constraints() -> None:
    """float(min=0.5, max=1.0).validator enforces the configured range."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = float(min=0.5, max=1.0)
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.validator(0.75)  # type: ignore[attr-defined]

    with pytest.raises(ValueError):
        data.validator(0.1)  # type: ignore[attr-defined]

    with pytest.raises(ValueError):
        data.validator(1.5)  # type: ignore[attr-defined]

    with pytest.raises(TypeError):
        data.validator(1)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 21. bool() factory returns a type struct with kind='type'
# ---------------------------------------------------------------------------


def test_bool_factory_returns_type_struct() -> None:
    """bool() with no kwargs returns the bool type struct."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = bool()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.name == "bool"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 22. integer() with no kwargs returns the type struct singleton
# ---------------------------------------------------------------------------


def test_integer_no_kwargs_returns_type_struct() -> None:
    """integer() with no kwargs returns the integer type struct (no sub-type)."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = integer()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.name == "integer"  # type: ignore[attr-defined]
    assert data.attributes == {}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 23. vector() is a typedef — accepts element_type, min_length, max_length
# ---------------------------------------------------------------------------


def test_vector_factory_via_typedef() -> None:
    """vector(element_type=integer()) returns a configured type struct."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = vector(element_type=integer(), min_length=1)
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.attributes["min_length"] == 1  # type: ignore[attr-defined]
    assert data.attributes["element_type"].name == "integer"  # type: ignore[attr-defined]


def test_vector_validator_accepts_valid_list() -> None:
    """vector(element_type=integer()).validator accepts a list of ints."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = vector(element_type=integer())
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator([1, 2, 3])  # type: ignore[attr-defined]


def test_vector_validator_rejects_wrong_element_type() -> None:
    """vector(element_type=integer()).validator rejects a list with non-int element."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = vector(element_type=integer())
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator([1, "oops", 3])  # type: ignore[attr-defined]


def test_vector_validator_enforces_min_length() -> None:
    """vector(element_type=integer(), min_length=2).validator rejects short lists."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = vector(element_type=integer(), min_length=2)
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator([1, 2])  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        data.validator([1])  # type: ignore[attr-defined]


def test_vector_without_element_type_returns_valid_struct() -> None:
    """vector() without element_type is valid now that element_type is optional."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = vector()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.name == "vector"  # type: ignore[attr-defined]


def test_vector_rejects_non_type_element_type() -> None:
    """vector(element_type=42) raises TypeError at factory call time."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        vector(element_type=42)
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError, match="element_type"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 24. tuple() is a typedef — bare type validates list/tuple values
# ---------------------------------------------------------------------------


def test_tuple_factory_via_typedef() -> None:
    """tuple() with no kwargs returns the tuple type struct."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = tuple()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.kind == "type"  # type: ignore[attr-defined]
    assert data.name == "tuple"  # type: ignore[attr-defined]


def test_tuple_validator_accepts_list() -> None:
    """tuple().validator accepts a plain list."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = tuple()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator([1, "a", True])  # type: ignore[attr-defined]


def test_tuple_validator_rejects_dict() -> None:
    """tuple().validator rejects a dict."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = tuple()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({"x": 1})  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 25. Positional tuple via list base: typedef(name="point", base=[float(), float()])
# ---------------------------------------------------------------------------


def test_positional_tuple_typedef() -> None:
    """typedef(name='point', base=[float(), float()]) creates a typed point type."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="point", base=[float(), float()])
        builtins.register("root", struct(name="r", t=point))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._types_by_name["point"]
    assert t.kind == "type"  # type: ignore[attr-defined]
    assert t.name == "point"  # type: ignore[attr-defined]


def test_positional_tuple_validator_accepts_correct_value() -> None:
    """point().validator accepts a 2-element list of floats."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="point", base=[float(), float()])
        builtins.register("root", struct(name="r", t=point()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.validator([1.0, 2.5])  # type: ignore[attr-defined]


def test_positional_tuple_validator_rejects_wrong_length() -> None:
    """point().validator rejects a list with wrong number of elements."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="point", base=[float(), float()])
        builtins.register("root", struct(name="r", t=point()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="length"):
        t.validator([1.0])  # type: ignore[attr-defined]


def test_positional_tuple_validator_rejects_wrong_element_type() -> None:
    """point().validator rejects a list where an element fails its type validator."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="point", base=[float(), float()])
        builtins.register("root", struct(name="r", t=point()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        t.validator([1.0, "not-a-float"])  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 26. typedef predicate — custom validator composed with base constraints
# ---------------------------------------------------------------------------


def test_typedef_predicate_valid_value() -> None:
    """typedef with predicate accepts a value satisfying both base and predicate."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "even_natural_number",
            base = integer(min=1),
            predicate = lambda v: v % 2 == 0,
        )
        builtins.register("root", struct(name="r", t=builtins.lookup("type", "even_natural_number")))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.validator(4)  # type: ignore[attr-defined]


def test_typedef_predicate_rejects_odd() -> None:
    """typedef predicate rejects an odd number even though it satisfies the base."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "even_natural_number",
            base = integer(min=1),
            predicate = lambda v: v % 2 == 0,
        )
        builtins.register("root", struct(name="r", t=builtins.lookup("type", "even_natural_number")))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        t.validator(3)  # type: ignore[attr-defined]


def test_typedef_predicate_base_constraint_still_applies() -> None:
    """Base constraint (min=1) rejects 0 even if it would pass the even predicate."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "even_natural_number",
            base = integer(min=1),
            predicate = lambda v: v % 2 == 0,
        )
        builtins.register("root", struct(name="r", t=builtins.lookup("type", "even_natural_number")))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        t.validator(0)  # type: ignore[attr-defined]


def test_positional_tuple_list_base_non_type_raises() -> None:
    """typedef with list base containing a non-type-struct raises TypeError."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(name="bad", base=[42])
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(TypeError):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 27. map() — uniform mode
# ---------------------------------------------------------------------------


def test_map_accepts_string_keyed_dict() -> None:
    """map() accepts a dict with string keys and any values."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"a": 1, "b": "x"})  # type: ignore[attr-defined]


def test_map_rejects_non_string_key_without_key_type() -> None:
    """map() rejects a dict with a non-string key when no key_type is set."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({1: "x"})  # type: ignore[attr-defined]


def test_map_rejects_non_dict() -> None:
    """map() rejects a non-dict value."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map()
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator([1, 2, 3])  # type: ignore[attr-defined]


def test_map_value_type_accepts_valid() -> None:
    """map(value_type=integer()) accepts {"a": 1}."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(value_type=integer())
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"a": 1})  # type: ignore[attr-defined]


def test_map_value_type_rejects_wrong_value() -> None:
    """map(value_type=integer()) rejects {"a": "x"}."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(value_type=integer())
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({"a": "x"})  # type: ignore[attr-defined]


def test_map_key_type_and_value_type() -> None:
    """map(key_type=integer(), value_type=string()) accepts {1: 'a'}, rejects {'a': 'b'}."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(key_type=integer(), value_type=string())
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({1: "a"})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({"a": "b"})  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 28. map() — per-key mode via map(fields=[...])
# ---------------------------------------------------------------------------


def test_map_fields_valid_dict_passes() -> None:
    """map(fields=[field(name='x', type=float()), ...]) accepts a valid dict."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float()), field(name="y", type=float())])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"x": 1.0, "y": 2.5})  # type: ignore[attr-defined]


def test_map_fields_missing_required_key_raises() -> None:
    """map(fields=[...]) raises ValueError when a required key is absent."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float()), field(name="y", type=float())])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="missing"):
        data.validator({"x": 1.0})  # type: ignore[attr-defined]


def test_map_fields_wrong_value_type_raises() -> None:
    """map(fields=[...]) raises TypeError when a field value has the wrong type."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float()), field(name="y", type=float())])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({"x": 1.0, "y": "bad"})  # type: ignore[attr-defined]


def test_map_fields_extra_key_allowed_by_default() -> None:
    """Extra keys are allowed by default (strict=False)."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float()), field(name="y", type=float())])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"x": 1.0, "y": 2.5, "z": 0.0})  # type: ignore[attr-defined]


def test_map_fields_strict_rejects_extra_key() -> None:
    """map(fields=[...], strict=True) raises ValueError on extra keys."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float())], strict=True)
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"x": 1.0})  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="unexpected"):
        data.validator({"x": 1.0, "extra": "oops"})  # type: ignore[attr-defined]


def test_map_fields_optional_field_not_required() -> None:
    """field(mandatory=False) — dict without that key passes."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(fields=[field(name="x", type=float()), field(name="label", type=string(), mandatory=False)])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({"x": 1.0})  # type: ignore[attr-defined]
    assert data.validator({"x": 1.0, "label": "A"})  # type: ignore[attr-defined]


def test_map_fields_with_integer_key_type() -> None:
    """map(key_type=integer(), fields=[field(name=0, ...)]) works with integer keys."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        result = map(key_type=integer(), fields=[field(name=0, type=string()), field(name=1, type=float())])
        builtins.register("root", struct(name="r", data=result))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert data.validator({0: "hello", 1: 3.14})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        data.validator({"a": "hello", "b": 3.14})  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 29. typedef(fields=[...]) — named per-key map types
# ---------------------------------------------------------------------------


def test_typedef_fields_registers_named_type() -> None:
    """typedef(name='point2d', fields=[...]) registers the type and injects a factory."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "point2d",
            fields = [field(name="x", type=float()), field(name="y", type=float())],
        )
        builtins.register("root", struct(name="r", t=point2d()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    assert "point2d" in ev._types_by_name
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.kind == "type"  # type: ignore[attr-defined]


def test_typedef_fields_validator_accepts_valid() -> None:
    """point2d().validator accepts a dict with x and y floats."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "point2d",
            fields = [field(name="x", type=float()), field(name="y", type=float())],
        )
        builtins.register("root", struct(name="r", t=point2d()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.validator({"x": 1.0, "y": 2.5})  # type: ignore[attr-defined]


def test_typedef_fields_extra_key_allowed_by_default() -> None:
    """Extra key 'z' is allowed when strict is not set."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "point2d",
            fields = [field(name="x", type=float()), field(name="y", type=float())],
        )
        builtins.register("root", struct(name="r", t=point2d()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.validator({"x": 1.0, "y": 2.5, "z": 0.0})  # type: ignore[attr-defined]


def test_typedef_fields_strict_rejects_extra_key() -> None:
    """typedef with strict=True rejects extra keys."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = dedent("""\
        load("//mlody/common/types.mlody")
        typedef(
            name = "named_point",
            fields = [
                field(name="x", type=float()),
                field(name="y", type=float()),
                field(name="label", type=string(), mandatory=False),
            ],
            strict = True,
        )
        builtins.register("root", struct(name="r", t=named_point()))
    """)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.validator({"x": 1.0, "y": 2.0})  # type: ignore[attr-defined]
    assert t.validator({"x": 1.0, "y": 2.0, "label": "A"})  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="unexpected"):
        t.validator({"x": 1.0, "y": 2.0, "extra": "oops"})  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# canonical() — new feature tests
# ---------------------------------------------------------------------------


def test_canonical_stored_on_type_struct() -> None:
    """canonical= is stored on the type struct and is callable."""
    ev = _eval("""\
        typedef(
            name = "email_address",
            base = string(),
            canonical = lambda v: v.strip().lower(),
        )
        builtins.register("root", struct(name="r", t=email_address()))
    """)
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert hasattr(t, "canonical"), "type struct should have .canonical"
    assert t.canonical("  USER@EXAMPLE.COM  ") == "user@example.com"  # type: ignore[attr-defined]


def test_canonical_inherited_by_derived_type() -> None:
    """A derived typedef inherits its base's canonical function."""
    ev = _eval("""\
        typedef(
            name = "email_address",
            base = string(),
            canonical = lambda v: v.strip().lower(),
        )
        typedef(
            name = "gmail_address",
            base = email_address(pattern=r".*@gmail\\.com"),
        )
        builtins.register("root", struct(name="r", t=gmail_address()))
    """)
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert hasattr(t, "canonical"), "derived type should inherit .canonical"
    assert t.canonical("  ME@GMAIL.COM  ") == "me@gmail.com"  # type: ignore[attr-defined]


def test_canonical_can_return_none() -> None:
    """canonical() returning None is valid (ambiguous / no canonical form)."""
    ev = _eval("""\
        typedef(
            name = "hex_prefix",
            base = string(),
            canonical = lambda v: v if len(v) == 40 else None,
        )
        builtins.register("root", struct(name="r", t=hex_prefix()))
    """)
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert t.canonical("abc123") is None  # type: ignore[attr-defined]
    assert t.canonical("a" * 40) == "a" * 40  # type: ignore[attr-defined]


def test_canonical_override_in_derived_type() -> None:
    """A derived typedef can replace its base's canonical function."""
    ev = _eval("""\
        typedef(
            name = "hex_prefix",
            base = string(),
            canonical = lambda v: v if len(v) == 40 else None,
        )
        typedef(
            name = "upper_hex",
            base = hex_prefix(),
            canonical = lambda v: v.upper() if len(v) == 40 else None,
        )
        builtins.register("root", struct(name="r", t=upper_hex()))
    """)
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    full_hash = "abcd" * 10
    assert t.canonical(full_hash) == full_hash.upper()  # type: ignore[attr-defined]
    assert t.canonical("short") is None  # type: ignore[attr-defined]


def test_canonical_on_fields_typedef() -> None:
    """A fields-based typedef can declare a canonical function."""
    ev = _eval("""\
        typedef(
            name = "point2d",
            fields = [field(name="x", type=float()), field(name="y", type=float())],
            canonical = lambda v: {"x": python.round(v["x"], 2), "y": python.round(v["y"], 2)},
        )
        builtins.register("root", struct(name="r", t=point2d()))
    """)
    t = ev._roots_by_name["r"].t  # type: ignore[attr-defined]
    assert hasattr(t, "canonical"), "fields typedef should store .canonical"
    result = t.canonical({"x": 1.123456, "y": 2.987654})  # type: ignore[attr-defined]
    assert result == {"x": 1.12, "y": 2.99}


# ---------------------------------------------------------------------------
# Abstract type hierarchy tests
# ---------------------------------------------------------------------------


def test_abstract_flag_on_hierarchy_roots() -> None:
    """top(), scalar(), aggregate() have abstract=True; integer(), string() have abstract=False."""
    ev = _eval("""\
        builtins.register("root", struct(
            name="r",
            top=top(),
            scalar=scalar(),
            aggregate=aggregate(),
            integer=integer(),
            string=string(),
        ))
    """)
    r = ev._roots_by_name["r"]  # type: ignore[attr-defined]
    assert r.top.abstract is True  # type: ignore[attr-defined]
    assert r.scalar.abstract is True  # type: ignore[attr-defined]
    assert r.aggregate.abstract is True  # type: ignore[attr-defined]
    assert r.integer.abstract is False  # type: ignore[attr-defined]
    assert r.string.abstract is False  # type: ignore[attr-defined]


def test_primitive_validators_unchanged_after_hierarchy() -> None:
    """All primitive validators still enforce their original constraints after the hierarchy change."""
    ev = _eval("""\
        builtins.register("root", struct(
            name="r",
            int_t=integer(),
            str_t=string(),
            float_t=float(),
            bool_t=bool(),
            vec_t=vector(element_type=integer()),
            tup_t=tuple(),
            map_t=map(),
        ))
    """)
    r = ev._roots_by_name["r"]  # type: ignore[attr-defined]
    assert r.int_t.validator(42)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.int_t.validator("not-int")  # type: ignore[attr-defined]

    assert r.str_t.validator("hello")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.str_t.validator(123)  # type: ignore[attr-defined]

    assert r.float_t.validator(3.14)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.float_t.validator("x")  # type: ignore[attr-defined]

    assert r.bool_t.validator(True)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.bool_t.validator(1)  # type: ignore[attr-defined]

    assert r.vec_t.validator([1, 2, 3])  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.vec_t.validator("not-a-list")  # type: ignore[attr-defined]

    assert r.tup_t.validator([1, "a"])  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.tup_t.validator("not-a-list")  # type: ignore[attr-defined]

    assert r.map_t.validator({"k": "v"})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        r.map_t.validator([1, 2])  # type: ignore[attr-defined]


def test_abstract_type_not_inherited_by_concrete() -> None:
    """integer (child of abstract scalar) has abstract=False, not True."""
    ev = _eval("""\
        builtins.register("root", struct(name="r", int_t=integer(), scalar_t=scalar()))
    """)
    r = ev._roots_by_name["r"]  # type: ignore[attr-defined]
    assert r.scalar_t.abstract is True  # type: ignore[attr-defined]
    assert r.int_t.abstract is False  # type: ignore[attr-defined]
