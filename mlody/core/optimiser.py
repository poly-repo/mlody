"""Query optimiser interface and sequential stub for mlody derived values.

Public surface:
- ``DerivedStep`` — frozen dataclass mirroring the ``derived`` location attrs
- ``QueryOptimiser`` — structural Protocol defining the optimiser interface
- ``SequentialOptimiser`` — trivial passthrough implementation (default)
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Sequence


@dataclasses.dataclass(frozen=True)
class DerivedStep:
    """An immutable step descriptor for one derived-value materialisation.

    Mirrors the fields on the ``derived`` location struct so that optimisers
    can inspect and rewrite the query plan without touching Starlark objects
    directly.

    Attributes:
        source_ref: The entity label string of the source value.
        sql_fragment: The raw SQL text (after dialect-tag removal).
        dialect: The query dialect identifier (e.g. ``"duckdb"``).
        output_path: The absolute cache file path for the materialised output.
    """

    source_ref: str
    sql_fragment: str
    dialect: str
    output_path: str


class QueryOptimiser(typing.Protocol):
    """Structural protocol for query plan optimisers.

    Implementors receive a sequence of ``DerivedStep`` objects and return a
    (possibly rewritten) sequence.  The trivial identity implementation is
    ``SequentialOptimiser``.  More sophisticated implementations may reorder,
    merge, or push down predicates.

    Structural subtyping is used (``typing.Protocol``) so external code can
    provide an implementation without inheriting from this class.
    """

    def optimise(self, steps: Sequence[DerivedStep]) -> Sequence[DerivedStep]:
        """Return an optimised version of ``steps``.

        Args:
            steps: The derivation steps to optimise, in their original order.

        Returns:
            A new sequence of ``DerivedStep`` objects, potentially reordered
            or rewritten.  Must not mutate the input.
        """
        ...


class SequentialOptimiser:
    """Trivial optimiser that returns steps in their original order unchanged.

    This is the default used by ``materialise_derived``.  It exists as a named
    class rather than a lambda so that callers can check
    ``isinstance(opt, SequentialOptimiser)`` if they want to detect the
    no-op case and skip the optimiser call for performance.
    """

    def optimise(self, steps: Sequence[DerivedStep]) -> Sequence[DerivedStep]:
        """Return a copy of ``steps`` in the same order with no modifications."""
        return list(steps)
