"""Base MlodyValue type, TypeCatalog, and is_registry_backed predicate.

This module has no rendering or engine dependencies — it may be imported in
isolation without pulling in render.py or engine/*.py as side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pyarrow as pa


# ---------------------------------------------------------------------------
# Type catalog (DQ-06)
# ---------------------------------------------------------------------------
#
# ``TypeCatalog`` owns the two lazy caches that previously lived as
# module-level mutable variables in label_value.py.  The singleton
# ``_TYPE_CATALOG`` is used by all internal call sites.  Tests construct fresh
# ``TypeCatalog()`` instances to obtain a clean cache state.


def _build_primitive_type_struct(name: str) -> object:
    """Build a minimal mlody primitive type struct for the given type name.

    The returned struct has ``kind="type"``, ``type=name``, ``name=name``,
    ``attributes={}``, and ``_allowed_attrs={}``.  This is sufficient for
    downstream type inspection and for building the ``vector(element_type=T)``
    wrapper struct.

    Args:
        name: One of ``"bool"``, ``"integer"``, ``"float"``, ``"string"``.

    Returns:
        A new Struct representing the named mlody primitive type.
    """
    from common.python.starlarkish.core.struct import Struct as _Struct  # noqa: PLC0415

    _root_kind_map = {
        "bool": "bool",
        "integer": "integer",
        "float": "float",
        # string inherits from aggregate in the real DSL (spec §3.3 table)
        "string": "aggregate",
    }
    return _Struct(
        kind="type",
        type=name,
        name=name,
        _root_kind=_root_kind_map.get(name, name),
        attributes={},
        _allowed_attrs={},
    )


class TypeCatalog:
    """Lazy singleton catalog for Arrow-to-mlody type mappings and primitive type structs.

    Encapsulates the two module-level mutable caches so they can be moved
    cleanly to ``resolver/values/base.py`` without altering call sites.
    """

    def __init__(self) -> None:
        self._arrow_map: "dict[pa.DataType, str] | None" = None
        self._primitive_structs: dict[str, object] = {}

    def arrow_type_name(self, arrow_type: "pa.DataType") -> "str | None":
        """Return the mlody type name for *arrow_type*, or ``None`` if unmapped.

        Builds the internal map on first call.  Importing pyarrow is deferred
        to avoid the cost on codepaths that never touch Parquet.
        """
        if self._arrow_map is None:
            import pyarrow as _pa  # noqa: PLC0415

            self._arrow_map = {
                _pa.bool_(): "bool",
                _pa.int8(): "integer",
                _pa.int16(): "integer",
                _pa.int32(): "integer",
                _pa.int64(): "integer",
                _pa.uint8(): "integer",
                _pa.uint16(): "integer",
                _pa.uint32(): "integer",
                _pa.uint64(): "integer",
                _pa.float16(): "float",
                _pa.float32(): "float",
                _pa.float64(): "float",
                _pa.string(): "string",
                _pa.large_string(): "string",
            }
        return self._arrow_map.get(arrow_type)

    def primitive_type_struct(self, name: str) -> object:
        """Return the cached mlody primitive type struct for *name*.

        Constructs the struct via ``_build_primitive_type_struct`` on first call
        per name.
        """
        if name not in self._primitive_structs:
            self._primitive_structs[name] = _build_primitive_type_struct(name)
        return self._primitive_structs[name]


_TYPE_CATALOG: TypeCatalog = TypeCatalog()


# ---------------------------------------------------------------------------
# Value base class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MlodyValue:
    """Base class for all resolved mlody values."""


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def is_registry_backed(value: object) -> bool:
    """Return True iff value is a registry-backed entity (task/action/user/value).

    Imported lazily from registry_backed to avoid a circular import at module
    load time — registry_backed imports MlodyValue from this module, so we
    must not import registry_backed here at the top level.
    """
    # Import here to avoid circular imports: registry_backed.py imports MlodyValue
    # from this module; importing it at top level would create a cycle.
    from mlody.resolver.values.registry_backed import (  # noqa: PLC0415
        MlodyActionValue,
        MlodyTaskValue,
        MlodyUserValue,
        MlodyValueValue,
    )

    return isinstance(value, (MlodyValueValue, MlodyTaskValue, MlodyActionValue, MlodyUserValue))
