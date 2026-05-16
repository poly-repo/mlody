"""Tests for mlody.extension — the unified registration surface.

Spec: openspec/changes/mlody-refactor-phase-3/specs/mlody-extension/spec.md

These tests verify:
- All public symbols are importable from mlody.extension
- _PATTERN_REGISTRY is accessible and is the same object as in mlody.core.multimethod
- No circular imports are introduced
- __all__ is correct (public names present, private names absent)
"""

import mlody.core.multimethod
import mlody.extension


class TestAllPublicSymbolsImportable:
    """Scenario: all public symbols are importable from mlody.extension."""

    def test_register_struct_base_importable(self) -> None:
        from mlody.extension import RegisteredStructBase

        assert RegisteredStructBase is not None

    def test_register_pattern_importable(self) -> None:
        from mlody.extension import register_pattern

        assert callable(register_pattern)

    def test_pattern_importable(self) -> None:
        from mlody.extension import Pattern

        assert Pattern is not None

    def test_register_step_engine_importable(self) -> None:
        from mlody.extension import register_step_engine

        assert callable(register_step_engine)

    def test_step_engine_importable(self) -> None:
        from mlody.extension import StepEngine

        assert StepEngine is not None

    def test_all_five_in_single_import(self) -> None:
        # Primary scenario: all five succeed in one import statement
        from mlody.extension import (  # noqa: F401
            Pattern,
            RegisteredStructBase,
            StepEngine,
            register_pattern,
            register_step_engine,
        )


class TestPatternRegistryAccessible:
    """Scenario: _PATTERN_REGISTRY is accessible from mlody.extension for diagnostics."""

    def test_pattern_registry_importable(self) -> None:
        from mlody.extension import _PATTERN_REGISTRY

        assert _PATTERN_REGISTRY is not None

    def test_pattern_registry_is_same_object(self) -> None:
        # Identity equality — must be the same dict, not a copy
        from mlody.extension import _PATTERN_REGISTRY

        assert _PATTERN_REGISTRY is mlody.core.multimethod._PATTERN_REGISTRY


class TestNoCircularImports:
    """Scenario: mlody.extension does not introduce circular imports."""

    def test_import_does_not_raise(self) -> None:
        # The import already happened at the top of this module;
        # confirm the module object is present and well-formed
        assert mlody.extension is not None
        assert mlody.extension.__doc__ is not None

    def test_import_after_resolver(self) -> None:
        # Scenario: mlody.extension can be imported after mlody.resolver
        import mlody.resolver  # noqa: F401 (import for side-effect verification)
        import mlody.extension as ext  # noqa: F401

        # Both imports must succeed (no AttributeError or ImportError raised)
        assert ext is not None


class TestDunderAll:
    """Scenario: __all__ lists public names; _PATTERN_REGISTRY is excluded."""

    def test_register_step_engine_in_all(self) -> None:
        assert "register_step_engine" in mlody.extension.__all__

    def test_step_engine_in_all(self) -> None:
        assert "StepEngine" in mlody.extension.__all__

    def test_register_struct_base_in_all(self) -> None:
        assert "RegisteredStructBase" in mlody.extension.__all__

    def test_register_pattern_in_all(self) -> None:
        assert "register_pattern" in mlody.extension.__all__

    def test_pattern_in_all(self) -> None:
        assert "Pattern" in mlody.extension.__all__

    def test_pattern_registry_not_in_all(self) -> None:
        # Private re-export — accessible but not advertised in __all__
        assert "_PATTERN_REGISTRY" not in mlody.extension.__all__


class TestDocstring:
    """Scenario: mlody.extension has a docstring explaining its purpose."""

    def test_module_has_docstring(self) -> None:
        assert mlody.extension.__doc__ is not None
        assert len(mlody.extension.__doc__.strip()) > 0

    def test_docstring_mentions_extension_points(self) -> None:
        doc = mlody.extension.__doc__ or ""
        # The docstring should convey that this is a registration surface
        assert "extension" in doc.lower() or "registration" in doc.lower()
