"""Typed tabular-source helpers for parquet-backed and derived mlody values."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

__all__ = [
    "CsvSource",
    "DerivedLocationSpec",
    "DerivedSource",
    "DerivedValueShapeError",
    "ParquetSource",
    "PosixLocationSpec",
    "PreviewResult",
    "QuerySpec",
    "RemoteLocationSpec",
    "TabularSource",
    "derived_location_spec_from_value",
    "source_from_location",
    "source_from_value",
    "validate_shape",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "CsvSource": ("mlody.core.tabular.csv_source", "CsvSource"),
    "DerivedLocationSpec": (
        "mlody.core.tabular.location_specs",
        "DerivedLocationSpec",
    ),
    "DerivedSource": ("mlody.core.tabular.derived_source", "DerivedSource"),
    "DerivedValueShapeError": (
        "mlody.core.tabular.derived_source",
        "DerivedValueShapeError",
    ),
    "ParquetSource": ("mlody.core.tabular.parquet_source", "ParquetSource"),
    "PosixLocationSpec": ("mlody.core.tabular.location_specs", "PosixLocationSpec"),
    "PreviewResult": ("mlody.core.tabular.interfaces", "PreviewResult"),
    "QuerySpec": ("mlody.core.query_spec", "QuerySpec"),
    "RemoteLocationSpec": ("mlody.core.tabular.location_specs", "RemoteLocationSpec"),
    "TabularSource": ("mlody.core.tabular.interfaces", "TabularSource"),
    "derived_location_spec_from_value": (
        "mlody.core.tabular.location_specs",
        "derived_location_spec_from_value",
    ),
    "source_from_location": (
        "mlody.core.tabular.location_specs",
        "source_from_location",
    ),
    "source_from_value": ("mlody.core.tabular.location_specs", "source_from_value"),
    "validate_shape": ("mlody.core.tabular.derived_source", "validate_shape"),
}


def __getattr__(name: str) -> object:
    """Load heavy tabular helpers on demand to avoid eager optional imports."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


if TYPE_CHECKING:
    from mlody.core.tabular.csv_source import CsvSource as CsvSource
    from mlody.core.tabular.derived_source import (
        DerivedSource as DerivedSource,
        DerivedValueShapeError as DerivedValueShapeError,
        validate_shape as validate_shape,
    )
    from mlody.core.query_spec import QuerySpec as QuerySpec
    from mlody.core.tabular.interfaces import (
        PreviewResult as PreviewResult,
        TabularSource as TabularSource,
    )
    from mlody.core.tabular.location_specs import (
        DerivedLocationSpec as DerivedLocationSpec,
        PosixLocationSpec as PosixLocationSpec,
        RemoteLocationSpec as RemoteLocationSpec,
        derived_location_spec_from_value as derived_location_spec_from_value,
        source_from_location as source_from_location,
        source_from_value as source_from_value,
    )
    from mlody.core.tabular.parquet_source import ParquetSource as ParquetSource
