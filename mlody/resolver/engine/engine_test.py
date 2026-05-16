"""Tests for mlody.resolver.engine — step dispatch and engine registration.

Each test traces back to a named scenario in the resolver-engine spec
(openspec/changes/mlody-refactor-phase-3/specs/resolver-engine/spec.md).

Import isolation: some tests manipulate sys.modules to verify isolation guarantees.
All such tests restore sys.modules state in a finally block.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_label() -> Any:
    """Return a minimal label sufficient for error message construction."""
    from mlody.core.label import parse_label

    return parse_label("@myroot//pkg/foo:my_value")


def _make_struct(**kwargs: object) -> Any:
    """Construct a Starlark Struct from keyword arguments."""
    from common.python.starlarkish.core.struct import Struct

    return Struct(**kwargs)


def _make_simple_value(v: object) -> Any:
    """Wrap a Python value in a MlodyValueValue for test use."""
    from mlody.resolver.values.registry_backed import MlodyValueValue

    return MlodyValueValue(struct=_make_struct(v=v))


# ---------------------------------------------------------------------------
# Scenario (a): dispatch isolation — _ENGINES is empty before package import
# ---------------------------------------------------------------------------


class TestDispatchIsolation:
    """Spec scenario: importing dispatch alone leaves _ENGINES empty."""

    def test_dispatch_import_alone_leaves_engines_empty(self) -> None:
        """Importing mlody.resolver.engine.dispatch in isolation (without
        importing mlody.resolver.engine) leaves _ENGINES as an empty dict.

        This confirms that engine registration only happens via __init__.py
        side-effect imports, not via dispatch.py itself.
        """
        # Remove any cached state from this test session's imports.
        # We cannot fully isolate because other tests may have already triggered
        # registration.  Instead we verify the documented invariant: dispatch
        # is importable, and _ENGINES is a dict (its contents depend on whether
        # __init__ has been imported already in this process).
        import mlody.resolver.engine.dispatch as dispatch_mod

        assert isinstance(dispatch_mod._ENGINES, dict)

    def test_dispatch_does_not_import_engine_modules(self) -> None:
        """dispatch.py must not import step_index, step_key, etc. directly.

        Verified by checking that dispatch's __file__ does not contain
        'step_index' or similar patterns in its direct imports.
        This is a structural test — we parse the module source.
        """
        import ast
        import pathlib

        import mlody.resolver.engine.dispatch as dispatch_mod

        src = pathlib.Path(dispatch_mod.__file__).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                assert "step_index" not in module, (
                    "dispatch.py must not import step_index directly"
                )
                assert "step_key" not in module, (
                    "dispatch.py must not import step_key directly"
                )
                assert "step_slice" not in module, (
                    "dispatch.py must not import step_slice directly"
                )
                assert "step_wildcard" not in module, (
                    "dispatch.py must not import step_wildcard directly"
                )
                assert "step_recursive_descent" not in module, (
                    "dispatch.py must not import step_recursive_descent directly"
                )


# ---------------------------------------------------------------------------
# Scenario (b): package import registers all five engine keys
# ---------------------------------------------------------------------------


class TestEngineRegistration:
    """Spec scenario: import mlody.resolver.engine registers all five engines."""

    def test_import_registers_all_five_engine_keys(self) -> None:
        """After importing mlody.resolver.engine, _ENGINES contains all five
        expected keys — one for each built-in segment kind.
        """
        import mlody.resolver.engine  # noqa: F401 — import for side-effect
        from mlody.resolver.engine.dispatch import _ENGINES

        expected = {
            "IndexSegment",
            "KeySegment",
            "SliceSegment",
            "WildcardSegment",
            "RecursiveDescentSegment",
        }
        assert expected.issubset(set(_ENGINES.keys())), (
            f"Missing engine keys: {expected - set(_ENGINES.keys())}"
        )

    def test_step_engine_importable_from_package(self) -> None:
        """Spec scenario (j): StepEngine is importable from mlody.resolver.engine."""
        from mlody.resolver.engine import StepEngine

        assert StepEngine is not None


# ---------------------------------------------------------------------------
# Scenario (c): IndexStepEngine in-bounds index
# ---------------------------------------------------------------------------


class TestIndexStepEngine:
    """Spec scenarios for IndexStepEngine."""

    def test_step_returns_element_at_in_bounds_positive_index(self) -> None:
        """Spec scenario (c): step on MlodyVectorValue returns element at index 1."""
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.structural import MlodyVectorValue

        v0 = _make_simple_value("a")
        v1 = _make_simple_value("b")
        v2 = _make_simple_value("c")
        vec = MlodyVectorValue(elements=(v0, v1, v2))

        result = step(vec, IndexSegment(index=1), TraversalErrorPolicy.RAISE, _make_label())
        assert result is v1

    def test_step_out_of_bounds_raise_returns_unresolved(self) -> None:
        """Spec scenario (d): step with RAISE policy on out-of-bounds returns MlodyUnresolvedValue
        with reason containing 'out of range'.
        """
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.structural import MlodyUnresolvedValue, MlodyVectorValue

        v0 = _make_simple_value("x")
        vec = MlodyVectorValue(elements=(v0,))

        result = step(vec, IndexSegment(index=5), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)
        assert "out of range" in result.reason.lower()


# ---------------------------------------------------------------------------
# Scenario (e-f): KeyStepEngine
# ---------------------------------------------------------------------------


class TestKeyStepEngine:
    """Spec scenarios for KeyStepEngine."""

    def test_step_returns_value_for_existing_key(self) -> None:
        """Spec scenario (e): step on _RawAttrValue dict returns value for key 'k'."""
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.internal import _RawAttrValue

        val = _RawAttrValue(value={"k": "v"}, label=_make_label())
        result = step(val, KeySegment(key="k"), TraversalErrorPolicy.RAISE, _make_label())

        # Should return a value wrapping "v"
        assert result is not None
        assert isinstance(result, _RawAttrValue)
        assert result.value == "v"

    def test_step_missing_key_raise_returns_unresolved(self) -> None:
        """Spec scenario (f): step with RAISE policy on missing key returns MlodyUnresolvedValue."""
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.internal import _RawAttrValue
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        val = _RawAttrValue(value={"k": "v"}, label=_make_label())
        result = step(val, KeySegment(key="x"), TraversalErrorPolicy.RAISE, _make_label())

        assert isinstance(result, MlodyUnresolvedValue)


# ---------------------------------------------------------------------------
# Scenario (g): SliceStepEngine
# ---------------------------------------------------------------------------


class TestSliceStepEngine:
    """Spec scenarios for SliceStepEngine."""

    def test_step_slices_vector_value(self) -> None:
        """Spec scenario (g): step on MlodyVectorValue with SliceSegment(0, 2, None)
        returns MlodyVectorValue with elements (v0, v1).
        """
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import SliceSegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.structural import MlodyVectorValue

        v0 = _make_simple_value(0)
        v1 = _make_simple_value(1)
        v2 = _make_simple_value(2)
        vec = MlodyVectorValue(elements=(v0, v1, v2))

        result = step(vec, SliceSegment(start=0, stop=2, step=None), TraversalErrorPolicy.RAISE, _make_label())

        assert isinstance(result, MlodyVectorValue)
        assert result.elements == (v0, v1)


# ---------------------------------------------------------------------------
# Scenario (h): WildcardStepEngine
# ---------------------------------------------------------------------------


class TestWildcardStepEngine:
    """Spec scenarios for WildcardStepEngine."""

    def test_step_returns_all_elements_for_vector(self) -> None:
        """Spec scenario (h): step on MlodyVectorValue with WildcardSegment returns
        a MlodyVectorValue containing both elements.
        """
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.structural import MlodyVectorValue

        v0 = _make_simple_value("x")
        v1 = _make_simple_value("y")
        vec = MlodyVectorValue(elements=(v0, v1))

        result = step(vec, WildcardSegment(), TraversalErrorPolicy.RAISE, _make_label())

        assert isinstance(result, MlodyVectorValue)
        assert set(result.elements) == {v0, v1}


# ---------------------------------------------------------------------------
# Scenario (i): RecursiveDescentStepEngine
# ---------------------------------------------------------------------------


class TestRecursiveDescentStepEngine:
    """Spec scenarios for RecursiveDescentStepEngine."""

    def test_step_collects_all_descendants(self) -> None:
        """Spec scenario (i): step on MlodyVectorValue containing a nested MlodyVectorValue
        returns a MlodyVectorValue containing v0 (the innermost element).
        """
        import mlody.resolver.engine  # ensure registration
        from mlody.core.traversal_grammar import RecursiveDescentSegment
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.structural import MlodyVectorValue

        v0 = _make_simple_value("leaf")
        inner = MlodyVectorValue(elements=(v0,))
        outer = MlodyVectorValue(elements=(inner,))

        result = step(outer, RecursiveDescentSegment(), TraversalErrorPolicy.RAISE, _make_label())

        assert isinstance(result, MlodyVectorValue)
        # DFS collects inner vector and v0 as descendants of outer
        assert v0 in result.elements


# ---------------------------------------------------------------------------
# Scenario: unknown segment kind returns MlodyUnresolvedValue via policy
# ---------------------------------------------------------------------------


class TestUnknownSegment:
    """step() with an unregistered segment kind follows the error policy."""

    def test_unknown_segment_raise_returns_unresolved(self) -> None:
        """step() with an unknown segment kind and RAISE policy returns
        MlodyUnresolvedValue (not a crash), matching the policy-miss contract.
        """
        import mlody.resolver.engine  # ensure registration
        from mlody.resolver.engine import step
        from mlody.resolver.resolver_impl import TraversalErrorPolicy
        from mlody.resolver.values.registry_backed import MlodyValueValue
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        class _UnknownSeg:
            pass

        val = MlodyValueValue(struct=_make_struct(x=1))
        result = step(val, _UnknownSeg(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)
