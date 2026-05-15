"""Location composition for record-typed value field traversal.

When ``Workspace.resolve()`` processes a label like ``:model.weights`` against
a record-typed value, the field's effective location is derived by composing the
parent value's location with the field's declared location.

This module owns:
- ``_LocationComposeError`` — private exception for unresolvable cases.
- ``LocationComposeFn`` — type alias for handler callables.
- ``_LOCATION_COMPOSERS`` — module-level dispatch table, accessible to tests.
- ``register_location_composer()`` — adds/replaces a handler in the table.
- ``compose_location()`` — main entry point; implements FR-008 composition rules.

The ``posix`` handler is registered at module import time (design D-7).

Design rationale (design.md D-1):
  Location composition lives in a dedicated module so ``workspace.py`` stays
  focused on loading and resolution, and so tests can reach the dispatch table
  without exposing workspace internals.

Design rationale (design.md D-5):
  ``compose_location()`` raises ``_LocationComposeError`` (not returns
  ``MlodyUnresolvedValue``) for unresolvable cases.  This keeps the module free
  of any dependency on ``mlody.resolver``.  ``Workspace.resolve()`` catches the
  exception and converts it.
"""

from __future__ import annotations

from typing import Callable

from mlody.common.struct import Struct
from mlody.core.location_specs import DerivedLocationSpec, PosixLocationSpec


class _LocationComposeError(Exception):
    """Raised by ``compose_location()`` for unresolvable cases.

    Never escapes resolver boundaries — caught and converted to
    ``MlodyUnresolvedValue`` (design D-2, D-5).
    """


#: Public alias for use by callers outside this module.
LocationComposeError = _LocationComposeError


# Type alias for composition handler callables.
# Signature: (parent_loc, field_loc_or_None, field_name) -> Struct
LocationComposeFn = Callable[["Struct", "Struct | None", str], "Struct"]

# Module-level dispatch table (design D-6).
# Keys are location kind strings (e.g. "posix").
# Accessible to tests for registering mock handlers.
_LOCATION_COMPOSERS: dict[str, LocationComposeFn] = {}

# Parent location kinds whose registered handlers accept field locations of a
# *different* kind (cross-kind composition).  All other cross-kind combinations
# continue to raise ``_LocationComposeError``.
_CROSS_KIND_PARENTS: frozenset[str] = frozenset({"derived"})


def register_location_composer(kind: str, fn: LocationComposeFn) -> None:
    """Add or replace a composition handler for ``kind`` in ``_LOCATION_COMPOSERS``."""
    _LOCATION_COMPOSERS[kind] = fn


def compose_location(
    parent_loc: Struct | None,
    field_loc: Struct | None,
    field_name: str,
) -> Struct | None:
    """Derive a field's effective location by composing parent and field locations.

    FR-008 composition rules (applied in order):

    1. Both None → return None.
    2. Parent None, field present → return field_loc unchanged.
    3. Parent present, field None → dispatch to parent kind handler with
       ``field_loc=None``; handler appends ``field_name`` to parent path.
    4. Both present, same kind → dispatch to registered handler.
    5. Both present, different kinds → raise ``_LocationComposeError``.
    6. Parent kind not registered → raise ``_LocationComposeError``.

    Returns:
        A ``Struct`` with ``kind="location"`` on success, or ``None`` when both
        inputs are ``None``.

    Raises:
        _LocationComposeError: for cross-kind or unregistered-kind cases.
    """
    if parent_loc is None and field_loc is None:
        return None

    if parent_loc is None:
        # Rule 2: parent absent, field present — return field location as-is.
        return field_loc

    # Parent is present for rules 3–6.
    # Use _root_kind (real mlody structs) or type, falling back to kind
    # (test fixtures that use kind="posix" directly).
    def _specific_kind(loc: object) -> str:
        return (
            getattr(loc, "_root_kind", None)
            or getattr(loc, "type", None)
            or getattr(loc, "kind", "")
        )

    parent_kind: str = _specific_kind(parent_loc)
    field_kind: str | None = _specific_kind(field_loc) if field_loc is not None else None

    if field_loc is not None and field_kind != parent_kind:
        # Rule 5: cross-kind — unsupported unless parent is in _CROSS_KIND_PARENTS.
        # Handlers in that set accept field locations of any kind (e.g. the
        # derived handler composes a derived parent with a posix field location).
        if parent_kind not in _CROSS_KIND_PARENTS:
            raise _LocationComposeError(
                f"Cannot compose location of kind {parent_kind!r} with field location "
                f"of kind {field_kind!r} for field {field_name!r}; "
                f"cross-kind composition is not supported."
            )

    handler = _LOCATION_COMPOSERS.get(parent_kind)
    if handler is None:
        # Rule 6: no handler registered for parent kind.
        raise _LocationComposeError(
            f"No composition handler registered for location kind {parent_kind!r} "
            f"(field: {field_name!r})."
        )

    # Rules 3 and 4: dispatch to the registered handler.
    return handler(parent_loc, field_loc, field_name)


def _posix_compose(
    parent_loc: Struct,
    field_loc: Struct | None,
    field_name: str,
) -> Struct:
    """Compose two posix locations by joining and expanding path lists.

    - Parent/field ``path`` values are normalized to lists of strings.
    - Parent list is joined with field list using cartesian composition.
    - Every composed element is glob-expanded.
    - The returned location always uses ``path`` as ``list[str]``.
    """
    parent_spec = PosixLocationSpec.from_location(parent_loc)
    if parent_spec is None:
        raise _LocationComposeError(
            f"Cannot compose invalid posix parent location {parent_loc!r}"
        )
    field_spec = PosixLocationSpec.from_location(field_loc) if field_loc is not None else None
    return parent_spec.compose(field_spec, field_name).to_struct()


register_location_composer("posix", _posix_compose)


# ---------------------------------------------------------------------------
# Built-in derived handler — registered at module import time
# ---------------------------------------------------------------------------


def _derived_compose(
    parent_loc: Struct,
    field_loc: Struct | None,
    field_name: str,
) -> Struct:
    """Compose a derived parent location with a (typically posix) field location.

    Takes the ``source_paths`` stored in the derived location's attributes,
    joins each with the field's relative path, expands any globs, deduplicates,
    and returns a new derived location with the composed absolute paths and a
    fresh deterministic cache hash.

    This allows field traversal on derived values (e.g.
    ``celebA-dataset-bald.valid``) to query the correct subdirectory of the
    materialised parquet dataset.
    """
    parent_spec = DerivedLocationSpec.from_location(parent_loc)
    if parent_spec is None:
        raise _LocationComposeError(
            f"Cannot compose invalid derived parent location {parent_loc!r}"
        )
    field_spec = PosixLocationSpec.from_location(field_loc) if field_loc is not None else None
    return parent_spec.compose(field_spec, field_name).to_struct()


register_location_composer("derived", _derived_compose)
