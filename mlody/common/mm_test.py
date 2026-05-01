"""Integration tests for mlody/common/mm.mlody.

Tests use a real Evaluator with InMemoryFS so that no evaluator internals are
mocked.  All scenarios listed in the mm-multimethod spec are covered here.

The helper _run_with_mm mirrors what WorkspaceLoader does in Phase 1:
  1. Evaluate mm.mlody.
  2. Populate evaluator._persistent_injections with `mm` and `defmethod`.
  3. Evaluate the user script — which sees mm/defmethod without any load().
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
from mlody.core.multimethod import DispatchError

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_MM_MLODY = (_THIS_DIR / "mm.mlody").read_text()

# Base files available in every test: rule.mlody (needed by mm.mlody) and mm.mlody itself.
_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/mm.mlody": _MM_MLODY,
}

_RENDER_MLODY = (_THIS_DIR / "render.mlody").read_text()


def _run_with_render(script: str) -> Evaluator:
    """Evaluate mm.mlody + render.mlody (in that order), then evaluate user script."""
    files = {
        **_BASE_FILES,
        "mlody/common/render.mlody": _RENDER_MLODY,
    }
    files["test.mlody"] = dedent(script)
    with InMemoryFS(files, root="/project") as root:
        ev = _make_evaluator_with_mm(files, root)
        ev.eval_file(root / "mlody" / "common" / "render.mlody")
        ev.eval_file(root / "test.mlody")
    return ev


def _make_evaluator_with_mm(
    files: dict[str, str],
    root: Path,
) -> Evaluator:
    """Return an Evaluator with mm.mlody evaluated and persistent injections set."""
    ev = Evaluator(root)
    mm_path = root / "mlody" / "common" / "mm.mlody"
    ev.eval_file(mm_path)
    # Propagate mm and defmethod as persistent injections, mirroring WorkspaceLoader.
    mm_globals = ev._module_globals.get(mm_path, {})
    for name in ("mm", "defmethod"):
        if name in mm_globals:
            ev._persistent_injections[name] = mm_globals[name]
    return ev


def _run_with_mm(script: str, extra_files: dict[str, str] | None = None) -> Evaluator:
    """Evaluate mm.mlody, set up persistent injections, then evaluate user script."""
    files = dict(_BASE_FILES)
    if extra_files:
        files.update(extra_files)
    files["test.mlody"] = dedent(script)
    with InMemoryFS(files, root="/project") as root:
        ev = _make_evaluator_with_mm(files, root)
        ev.eval_file(root / "test.mlody")
    return ev


def _run_with_mm_raises(
    script: str,
    exc_type: type[BaseException],
    *,
    match: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> None:
    """Assert that evaluating script (with mm pre-loaded) raises exc_type."""
    files = dict(_BASE_FILES)
    if extra_files:
        files.update(extra_files)
    files["test.mlody"] = dedent(script)
    with InMemoryFS(files, root="/project") as root:
        ev = _make_evaluator_with_mm(files, root)
        if match:
            with pytest.raises(exc_type, match=match):
                ev.eval_file(root / "test.mlody")
        else:
            with pytest.raises(exc_type):
                ev.eval_file(root / "test.mlody")


def _globals_of(ev: Evaluator, filename: str = "test.mlody") -> dict[str, object]:
    """Return the sandbox globals dict for the given file."""
    key = Path("/project") / filename
    return ev._module_globals.get(key, {})


# ---------------------------------------------------------------------------
# Scenario: mm available without explicit load
# Ref: Requirement 'mm namespace struct injected into the sandbox'
# ---------------------------------------------------------------------------


def test_mm_injected_into_sandbox_without_load() -> None:
    """mm is bound in the user file's globals after mm.mlody is pre-loaded."""
    ev = _run_with_mm("x = mm")
    g = _globals_of(ev)
    assert "mm" in g


def test_mm_has_all_required_attributes() -> None:
    """mm exposes generic, method, ANY, value, vector, posix, json, T."""
    ev = _run_with_mm("x = mm")
    g = _globals_of(ev)
    mm = g["mm"]
    for attr in ("generic", "method", "ANY", "value", "vector", "posix", "json", "T"):
        assert hasattr(mm, attr), f"mm is missing attribute {attr!r}"


# ---------------------------------------------------------------------------
# Scenario: mm importable via explicit load
# Ref: 'mm importable via explicit load'
# ---------------------------------------------------------------------------


def test_mm_importable_via_load() -> None:
    """load('//mlody/common/mm.mlody', 'mm') binds the mm struct."""
    script = 'load("//mlody/common/mm.mlody", "mm")\nloaded_mm = mm'
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    g = ev._module_globals[root / "test.mlody"]
    assert "loaded_mm" in g
    loaded = g["loaded_mm"]
    for attr in ("generic", "method", "ANY"):
        assert hasattr(loaded, attr), f"loaded mm missing {attr!r}"


# ---------------------------------------------------------------------------
# Scenario: Top-level names not polluted
# Ref: 'Top-level names not polluted'
# ---------------------------------------------------------------------------


def test_top_level_names_not_polluted() -> None:
    """generic, method, ANY, etc. are NOT bound as top-level sandbox names."""
    ev = _run_with_mm("pass")
    g = _globals_of(ev)
    for name in ("generic", "method", "ANY", "value", "vector", "posix", "json", "T"):
        assert name not in g, f"unexpected top-level name {name!r}"


def test_user_generic_variable_does_not_conflict() -> None:
    """A user variable named 'generic' does not clash with mm.generic."""
    ev = _run_with_mm("generic = 42")
    g = _globals_of(ev)
    assert g["generic"] == 42
    # mm is still intact
    assert hasattr(g["mm"], "generic")


# ---------------------------------------------------------------------------
# Scenario: mm.ANY not callable
# Ref: Requirement 'mm.ANY wildcard sentinel constant'
# ---------------------------------------------------------------------------


def test_mm_any_not_callable() -> None:
    """mm.ANY is not callable; calling it raises TypeError."""
    _run_with_mm_raises("mm.ANY()", TypeError)


def test_mm_any_is_struct_kind_mm_any() -> None:
    """mm.ANY has kind='mm_any'."""
    ev = _run_with_mm("x = mm.ANY")
    g = _globals_of(ev)
    assert getattr(g["x"], "kind") == "mm_any"


# ---------------------------------------------------------------------------
# Scenario: mm.T empty name rejected
# Ref: Requirement 'mm.T(scalar_name) scalar type pattern'
# ---------------------------------------------------------------------------


def test_mm_T_empty_name_raises_value_error() -> None:
    """mm.T('') raises ValueError."""
    _run_with_mm_raises('mm.T("")', ValueError)


# ---------------------------------------------------------------------------
# Scenario: mm.json not callable
# Ref: Requirement 'mm.json JSON representation kind constant'
# ---------------------------------------------------------------------------


def test_mm_json_not_callable() -> None:
    """mm.json is not callable; calling mm.json() raises TypeError."""
    _run_with_mm_raises("mm.json()", TypeError)


# ---------------------------------------------------------------------------
# Scenario: generic() declare and call
# Ref: Requirement 'generic() rule declares a named multimethod'
# ---------------------------------------------------------------------------


def test_generic_returns_callable() -> None:
    """mm.generic('render') returns a callable dispatch function."""
    ev = _run_with_mm("render = mm.generic('render')")
    g = _globals_of(ev)
    assert callable(g["render"])


def test_generic_registered_in_evaluator() -> None:
    """The struct with kind='generic' and name='render' is in ev.generics."""
    ev = _run_with_mm("render = mm.generic('render')")
    assert "render" in ev._generics_by_name
    g_struct = ev._generics_by_name["render"]
    assert getattr(g_struct, "kind") == "generic"
    assert getattr(g_struct, "name") == "render"


def test_generic_with_description_stored() -> None:
    """generic(description=...) stores description in the registered struct."""
    ev = _run_with_mm("render = mm.generic('render', description='Render output')")
    g_struct = ev._generics_by_name["render"]
    assert getattr(g_struct, "description") == "Render output"


def test_generic_empty_name_raises_value_error() -> None:
    """mm.generic('') raises ValueError."""
    _run_with_mm_raises("mm.generic('')", ValueError)


# ---------------------------------------------------------------------------
# Scenario: method() attaches implementation and dispatches correctly
# Ref: Requirement 'method() rule attaches a concrete implementation'
# ---------------------------------------------------------------------------


def test_method_registers_and_dispatch_calls_body() -> None:
    """mm.method registers a method; calling the generic invokes it.

    Ref: Scenario 'Attach a method with an exact-string pattern'.
    """
    ev = _run_with_mm(dedent("""\
        render = mm.generic("render")
        _calls = []
        def _fn(ctx, arg):
            _calls.append(arg)
        mm.method(generic=render, patterns=["train"], body=_fn)
        render("train")
    """))
    g = _globals_of(ev)
    assert g["_calls"] == ["train"]


def test_method_non_callable_body_raises_type_error() -> None:
    """mm.method with a non-callable body raises TypeError at registration time."""
    _run_with_mm_raises(
        dedent("""\
            render = mm.generic("render")
            mm.method(generic=render, patterns=["train"], body="not_a_fn")
        """),
        TypeError,
    )


def test_method_eager_label_resolution_failure_raises_value_error() -> None:
    """mm.method with an unresolvable ':nonexistent' label raises ValueError."""
    _run_with_mm_raises(
        dedent("""\
            render = mm.generic("render")
            def _fn(ctx, arg):
                pass
            mm.method(generic=render, patterns=[":nonexistent"], body=_fn)
        """),
        ValueError,
        match="nonexistent",
    )


# ---------------------------------------------------------------------------
# Scenario: Arity inference and enforcement
# Ref: Requirement 'Arity inference and enforcement'
# ---------------------------------------------------------------------------


def test_mixed_arity_raises_value_error() -> None:
    """Attaching a method with wrong arity raises ValueError naming the generic."""
    _run_with_mm_raises(
        dedent("""\
            g = mm.generic("proc")
            def _fn(ctx, *args):
                pass
            mm.method(generic=g, patterns=["train"], body=_fn)
            mm.method(generic=g, patterns=["train", "gpu"], body=_fn)
        """),
        ValueError,
        match="proc",
    )


def test_zero_method_generic_raises_dispatch_error() -> None:
    """Calling a generic with no methods raises DispatchError (not a crash).

    Ref: Scenario 'Zero-method generic dispatches cleanly'.
    """
    _run_with_mm_raises(
        dedent("""\
            g = mm.generic("empty")
            g("anything")
        """),
        DispatchError,
    )


# ---------------------------------------------------------------------------
# Scenario: defmethod top-level shorthand
# Ref: Requirement 'defmethod top-level shorthand'
# ---------------------------------------------------------------------------


def test_defmethod_is_top_level_sandbox_name() -> None:
    """defmethod is bound in the global scope without any explicit load."""
    ev = _run_with_mm("x = defmethod")
    g = _globals_of(ev)
    assert callable(g["x"])


def test_defmethod_registers_identically_to_mm_method() -> None:
    """defmethod registers a method identically to mm.method.

    Ref: Scenario 'defmethod registers a method identically to mm.method'.
    """
    ev = _run_with_mm(dedent("""\
        render = mm.generic("render")
        _calls = []
        def _fn(ctx, arg):
            _calls.append(("defmethod", arg))
        defmethod(render, ["train"], _fn)
        render("train")
    """))
    g = _globals_of(ev)
    assert g["_calls"] == [("defmethod", "train")]


# ---------------------------------------------------------------------------
# Scenario: Specificity ordering
# Ref: Requirement 'Specificity-based dispatch ordering'
# ---------------------------------------------------------------------------


def test_most_specific_method_wins() -> None:
    """The method with highest total score is selected.

    Ref: Scenario 'Most specific method wins' from spec.
    """
    ev = _run_with_mm(dedent("""\
        proc = mm.generic("proc")
        _selected = []
        def _exact(ctx, a, b): _selected.append("exact")
        def _any_fn(ctx, a, b): _selected.append("any")
        mm.method(generic=proc, patterns=["train", "gpu"], body=_exact)
        mm.method(generic=proc, patterns=["train", mm.ANY], body=_any_fn)
        mm.method(generic=proc, patterns=[mm.ANY, "gpu"], body=_any_fn)
        mm.method(generic=proc, patterns=[mm.ANY, mm.ANY], body=_any_fn)
        proc("train", "gpu")
    """))
    g = _globals_of(ev)
    assert g["_selected"] == ["exact"]


def test_first_registered_wins_on_tie() -> None:
    """On equal score, the first-registered method is called.

    Ref: Scenario 'First-registered wins on tie'.
    """
    ev = _run_with_mm(dedent("""\
        g = mm.generic("g")
        _selected = []
        def _first(ctx, a): _selected.append("first")
        def _second(ctx, a): _selected.append("second")
        mm.method(generic=g, patterns=[mm.ANY], body=_first)
        mm.method(generic=g, patterns=[mm.ANY], body=_second)
        g("anything")
    """))
    g = _globals_of(ev)
    assert g["_selected"] == ["first"]


def test_composite_score_most_constrained_wins() -> None:
    """Most-constrained composite pattern wins over less-constrained one.

    Ref: Scenario 'Most constrained composite beats least constrained composite'.
    """
    from common.python.starlarkish.core.struct import Struct as _Struct

    def _make(**kw: object) -> _Struct:
        return _Struct(**kw)  # type: ignore[arg-type]

    string_type = _make(kind="type", type_name="string", name="string")
    string_vec = _make(kind="type", type_name="vector", name="vector", element_type=string_type)
    json_repr = _make(kind="representation", repr_name="json")
    value_arg = _make(kind="value", type=string_vec, representation=json_repr)

    results: list[str] = []

    from mlody.core.multimethod import dispatch
    from common.python.starlarkish.core.struct import Struct

    def _specific(ctx: object, *args: object) -> str:
        results.append("specific")
        return "specific"

    def _general(ctx: object, *args: object) -> str:
        results.append("general")
        return "general"

    T_string = _make(kind="mm_scalar_pattern", type_name="string")
    mm_any = _make(kind="mm_any")
    mm_json = _make(kind="mm_repr_pattern", repr_name="json")

    specific_pat = _make(
        kind="mm_value_pattern",
        fields={
            "type": _make(kind="mm_vector_pattern", element_type=T_string),
            "representation": mm_json,
        },
    )
    general_pat = _make(
        kind="mm_value_pattern",
        fields={
            "type": _make(kind="mm_vector_pattern", element_type=mm_any),
            "representation": mm_json,
        },
    )
    specific_method = Struct(kind="method", patterns=[specific_pat], body=_specific)  # type: ignore[arg-type]
    general_method = Struct(kind="method", patterns=[general_pat], body=_general)  # type: ignore[arg-type]

    ret = dispatch("render", (value_arg,), [specific_method, general_method])
    assert ret == "specific"
    assert results == ["specific"]


# ---------------------------------------------------------------------------
# render_value — celebA-row vector renderer
# ---------------------------------------------------------------------------


def test_celebA_vector_renderer_two_column_preview() -> None:
    """render_value on a vector-of-celebA-row produces image + attributes columns."""
    from mlody.core.multimethod import dispatch
    from common.python.starlarkish.core.struct import Struct

    element_type = Struct(kind="type", name="celebA-row", type_name="celebA-row")
    type_struct = Struct(kind="type", name="vector", type_name="vector", element_type=element_type)
    value_struct = Struct(
        kind="value",
        name="test-dataset",
        type=type_struct,
        _tabular_preview=(
            ["image", "Bald", "Young"],
            [
                ["<img1>", "False", "True"],
                ["<img2>", "True", "False"],
            ],
            2,
        ),
    )

    ev = _run_with_render("pass")
    methods = list(ev._method_registry.get("render_value", {}).get("methods", []))
    spec = dispatch("render_value", (value_struct,), methods)

    assert spec.kind == "render_value_spec"
    assert len(spec.sections) == 1
    col_names, data_rows, total_rows = spec.sections[0].tabular_preview
    assert col_names == ["image", "attributes"]
    assert data_rows[0] == ["<img1>", "Young"]
    assert data_rows[1] == ["<img2>", "Bald"]
    assert total_rows == 2


def test_celebA_vector_renderer_no_preview_when_absent() -> None:
    """render_value on a celebA-row vector with no _tabular_preview returns empty tabular."""
    from mlody.core.multimethod import dispatch
    from common.python.starlarkish.core.struct import Struct

    element_type = Struct(kind="type", name="celebA-row", type_name="celebA-row")
    type_struct = Struct(kind="type", name="vector", type_name="vector", element_type=element_type)
    value_struct = Struct(
        kind="value",
        name="test-dataset",
        type=type_struct,
    )

    ev = _run_with_render("pass")
    methods = list(ev._method_registry.get("render_value", {}).get("methods", []))
    spec = dispatch("render_value", (value_struct,), methods)

    assert spec.kind == "render_value_spec"
    assert len(spec.sections) == 1
    assert spec.sections[0].tabular_preview is None
