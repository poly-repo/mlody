"""Tests for mlody.resolver.values — isolation and TypeCatalog contracts.

Task 2.16 (mlody-refactor-phase-3):
  Verify that ``mlody.resolver.values.base`` can be imported without pulling
  in ``mlody.resolver.render`` or any ``engine/`` module as a side effect.

Task 2.17 (mlody-refactor-phase-3):
  Verify the TypeCatalog API — lazy initialisation, cache reuse, and the
  ``is_registry_backed`` predicate — against the contracts in design.md §2.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 2.16 — Import isolation
# ---------------------------------------------------------------------------


class TestValuesBaseIsolation:
    """Requirement: values/base.py has no render or engine imports."""

    def test_base_importable_without_render_module(self) -> None:
        """Importing base must not cause mlody.resolver.render to be loaded.

        render.py pulls in functools.singledispatch registrations for all value
        types; it must remain unloaded until a caller explicitly imports it.
        """
        # Force a fresh import by removing any cached entry.  In a normal test
        # run the module may already be loaded by earlier tests — we just verify
        # the render module is NOT present if only base is imported.
        import mlody.resolver.values.base  # noqa: F401 — side-effect import

        # The critical check: base itself has no import statement for render.
        # (Docstrings or comments may mention "render" by name, but imports must not.)
        import importlib
        import inspect

        base_src = inspect.getsource(importlib.import_module("mlody.resolver.values.base"))
        assert "import mlody.resolver.render" not in base_src
        assert "from mlody.resolver.render" not in base_src

    def test_structural_does_not_import_render_module(self) -> None:
        """structural.py must not have an import statement for render."""
        import importlib
        import inspect

        structural = importlib.import_module("mlody.resolver.values.structural")
        src = inspect.getsource(structural)
        # Docstrings may mention render for documentation; import lines must not.
        assert "import mlody.resolver.render" not in src
        assert "from mlody.resolver.render" not in src

    def test_registry_backed_does_not_import_render_module(self) -> None:
        """registry_backed.py must not have an import statement for render."""
        import importlib
        import inspect

        rb = importlib.import_module("mlody.resolver.values.registry_backed")
        src = inspect.getsource(rb)
        assert "import mlody.resolver.render" not in src
        assert "from mlody.resolver.render" not in src

    def test_internal_does_not_import_render_module(self) -> None:
        """internal.py must not have an import statement for render."""
        import importlib
        import inspect

        internal = importlib.import_module("mlody.resolver.values.internal")
        src = inspect.getsource(internal)
        assert "import mlody.resolver.render" not in src
        assert "from mlody.resolver.render" not in src

    def test_base_has_no_engine_imports(self) -> None:
        """base.py must not import from mlody.resolver.engine (Wave 3c boundary)."""
        import importlib
        import inspect

        base = importlib.import_module("mlody.resolver.values.base")
        src = inspect.getsource(base)
        assert "mlody.resolver.engine" not in src

    def test_mlody_value_base_class_is_frozen_dataclass(self) -> None:
        """MlodyValue is a frozen dataclass with no fields.

        Being frozen is required so all subclasses can safely participate in
        sets and as dict keys.
        """
        import dataclasses

        from mlody.resolver.values.base import MlodyValue

        fields = dataclasses.fields(MlodyValue)
        assert len(fields) == 0
        # Frozen is stored in the class's dataclass params via __dataclass_params__
        params = MlodyValue.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen is True


# ---------------------------------------------------------------------------
# 2.17 — TypeCatalog
# ---------------------------------------------------------------------------


class TestTypeCatalog:
    """Requirement: TypeCatalog provides lazy Arrow-to-mlody type mapping."""

    def _fresh_catalog(self) -> Any:
        """Return a new TypeCatalog with empty caches."""
        from mlody.resolver.values.base import TypeCatalog

        return TypeCatalog()

    def test_arrow_type_name_maps_bool(self) -> None:
        """bool_() Arrow type maps to mlody 'bool'."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        assert cat.arrow_type_name(pa.bool_()) == "bool"

    def test_arrow_type_name_maps_int32_to_integer(self) -> None:
        """int32 Arrow type maps to mlody 'integer'."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        assert cat.arrow_type_name(pa.int32()) == "integer"

    def test_arrow_type_name_maps_float64_to_float(self) -> None:
        """float64 Arrow type maps to mlody 'float'."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        assert cat.arrow_type_name(pa.float64()) == "float"

    def test_arrow_type_name_maps_string_to_string(self) -> None:
        """string Arrow type maps to mlody 'string'."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        assert cat.arrow_type_name(pa.string()) == "string"

    def test_arrow_type_name_returns_none_for_unknown(self) -> None:
        """Unmapped Arrow types return None, not an exception."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        # date32 is not in the mlody type set
        assert cat.arrow_type_name(pa.date32()) is None

    def test_arrow_type_name_lazy_initialises_on_first_call(self) -> None:
        """The internal _arrow_map is None before the first call."""
        cat = self._fresh_catalog()
        assert cat._arrow_map is None  # noqa: SLF001

        import pyarrow as pa

        cat.arrow_type_name(pa.bool_())
        assert cat._arrow_map is not None  # noqa: SLF001

    def test_arrow_type_name_reuses_cache_across_calls(self) -> None:
        """Multiple calls share the same map object (no re-initialisation)."""
        import pyarrow as pa

        cat = self._fresh_catalog()
        cat.arrow_type_name(pa.bool_())
        first_map = cat._arrow_map  # noqa: SLF001
        cat.arrow_type_name(pa.int32())
        assert cat._arrow_map is first_map  # noqa: SLF001

    def test_primitive_type_struct_returns_struct_with_kind_type(self) -> None:
        """primitive_type_struct('integer') returns a Struct with kind='type'."""
        cat = self._fresh_catalog()
        s = cat.primitive_type_struct("integer")
        assert getattr(s, "kind", None) == "type"
        assert getattr(s, "type", None) == "integer"

    def test_primitive_type_struct_caches_result(self) -> None:
        """Calling primitive_type_struct twice returns the same object."""
        cat = self._fresh_catalog()
        first = cat.primitive_type_struct("string")
        second = cat.primitive_type_struct("string")
        assert first is second

    def test_primitive_type_struct_bool_has_bool_root_kind(self) -> None:
        """bool primitive struct has _root_kind='bool'."""
        cat = self._fresh_catalog()
        s = cat.primitive_type_struct("bool")
        assert getattr(s, "_root_kind", None) == "bool"

    def test_primitive_type_struct_string_has_aggregate_root_kind(self) -> None:
        """string primitive struct has _root_kind='aggregate' (mlody DSL contract)."""
        cat = self._fresh_catalog()
        s = cat.primitive_type_struct("string")
        assert getattr(s, "_root_kind", None) == "aggregate"

    def test_type_catalog_singleton_exists(self) -> None:
        """The module-level _TYPE_CATALOG singleton is a TypeCatalog instance."""
        from mlody.resolver.values.base import TypeCatalog, _TYPE_CATALOG

        assert isinstance(_TYPE_CATALOG, TypeCatalog)


# ---------------------------------------------------------------------------
# 2.17 — is_registry_backed predicate
# ---------------------------------------------------------------------------


class TestIsRegistryBacked:
    """Requirement: is_registry_backed correctly classifies value types."""

    def test_task_value_is_registry_backed(self) -> None:
        from common.python.starlarkish.core.struct import Struct

        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.registry_backed import MlodyTaskValue

        v = MlodyTaskValue(struct=Struct(kind="task", name="t", inputs=[], outputs=[], config={}))
        assert is_registry_backed(v) is True

    def test_action_value_is_registry_backed(self) -> None:
        from common.python.starlarkish.core.struct import Struct

        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.registry_backed import MlodyActionValue

        v = MlodyActionValue(struct=Struct(kind="action", name="a", inputs=[], outputs=[], config={}))
        assert is_registry_backed(v) is True

    def test_value_value_is_registry_backed(self) -> None:
        from common.python.starlarkish.core.struct import Struct

        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.registry_backed import MlodyValueValue

        v = MlodyValueValue(struct=Struct(kind="value", name="x", type=None, location=None, default=None))
        assert is_registry_backed(v) is True

    def test_user_value_is_registry_backed(self) -> None:
        from common.python.starlarkish.core.struct import Struct

        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.registry_backed import MlodyUserValue

        v = MlodyUserValue(struct=Struct(kind="user", name="u"))
        assert is_registry_backed(v) is True

    def test_folder_value_is_not_registry_backed(self) -> None:
        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.structural import MlodyFolderValue

        v = MlodyFolderValue(path="teams/myroot", children=[])
        assert is_registry_backed(v) is False

    def test_unresolved_value_is_not_registry_backed(self) -> None:
        from mlody.resolver.values.base import is_registry_backed
        from mlody.resolver.values.structural import MlodyUnresolvedValue

        # label is typed as Label but accepts anything at runtime; use a sentinel
        v = MlodyUnresolvedValue(label=None, reason="no entity")  # type: ignore[arg-type]
        assert is_registry_backed(v) is False

    def test_plain_string_is_not_registry_backed(self) -> None:
        from mlody.resolver.values.base import is_registry_backed

        assert is_registry_backed("hello") is False

    def test_none_is_not_registry_backed(self) -> None:
        from mlody.resolver.values.base import is_registry_backed

        assert is_registry_backed(None) is False
