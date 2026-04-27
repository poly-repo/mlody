"""Typed helpers for derived-value materialisation."""

from __future__ import annotations

from mlody.core.optimiser import QueryOptimiser, SequentialOptimiser
from mlody.core.tabular.derived_source import (
    DerivedSource,
    DerivedValueShapeError,
    validate_shape,
)
from mlody.core.tabular.location_specs import DerivedLocationSpec

_DEFAULT_OPTIMISER = SequentialOptimiser()

def materialise_derived(
    source: DerivedSource | DerivedLocationSpec,
    *,
    optimiser: QueryOptimiser = _DEFAULT_OPTIMISER,
) -> str:
    """Materialise a derived value and return the cached parquet path.

    The typed entry points are:
    - ``materialise_derived(DerivedSource(...))``
    - ``materialise_derived(DerivedLocationSpec(...))``
    """
    if isinstance(source, DerivedSource):
        return str(source.materialize())

    return str(DerivedSource(spec=source, optimiser=optimiser).materialize())
