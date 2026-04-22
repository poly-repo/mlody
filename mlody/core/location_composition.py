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

import glob
import hashlib
import os
from typing import Callable

from common.python.starlarkish.core.struct import Struct


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


# ---------------------------------------------------------------------------
# Built-in posix handler (design D-7: registered at module import time)
# ---------------------------------------------------------------------------


def _as_path_list(path_value: object) -> list[str]:
    """Coerce a location ``path`` value to a list of strings."""
    if path_value is None:
        return []
    if isinstance(path_value, str):
        return [path_value]
    if isinstance(path_value, (list, tuple)):
        return [str(p) for p in path_value]
    return [str(path_value)]


def _get_paths(loc: Struct) -> list[str]:
    """Extract ``path`` from a location Struct as a list of strings.

    Location structs produced by the mlody evaluator store ``path`` inside
    the ``attributes`` dict (via ``extend_attrs``).  Test fixtures may store
    it as a direct top-level field.  Check both.
    """
    direct = getattr(loc, "path", None)
    if direct is not None:
        return _as_path_list(direct)
    attrs = getattr(loc, "attributes", None)
    if isinstance(attrs, dict):
        return _as_path_list(attrs.get("path"))
    return []


def _expand_glob(path_pattern: str) -> list[str]:
    """Expand ``path_pattern`` if it contains glob syntax."""
    expanded_pattern = os.path.expanduser(path_pattern)
    if not glob.has_magic(expanded_pattern):
        return [path_pattern]
    return sorted(glob.glob(expanded_pattern))


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
    parent_paths = _get_paths(parent_loc)
    if not parent_paths:
        parent_paths = [""]

    field_paths = _get_paths(field_loc) if field_loc is not None else [field_name]
    if not field_paths:
        field_paths = [field_name]

    composed_patterns = [
        os.path.join(parent_path, field_path)
        for parent_path in parent_paths
        for field_path in field_paths
    ]
    expanded_paths = [p for pattern in composed_patterns for p in _expand_glob(pattern)]
    # If no globs matched anything, keep the raw composed paths.
    if not expanded_paths:
        expanded_paths = composed_patterns

    # Preserve order while deduplicating.
    seen: set[str] = set()
    composed_path = [p for p in expanded_paths if not (p in seen or seen.add(p))]
    return Struct(
        kind="location",
        type="posix",
        name=getattr(parent_loc, "name", ""),
        path=composed_path,
    )


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
    attrs: dict[str, object] = getattr(parent_loc, "attributes", {}) or {}
    source_paths: list[str] = list(attrs.get("source_paths") or [])  # type: ignore[arg-type]
    sql_fragment: str = str(attrs.get("sql_fragment") or "")
    dialect: str = str(attrs.get("dialect") or "duckdb")
    source_ref: str = str(attrs.get("source_ref") or "")

    field_paths: list[str] = _get_paths(field_loc) if field_loc is not None else [field_name]
    if not field_paths:
        field_paths = [field_name]

    # Cartesian composition: join each source path with each field path.
    composed_patterns = [
        os.path.join(os.path.expanduser(sp), fp)
        for sp in (source_paths or [""])
        for fp in field_paths
    ]

    expanded_paths = [p for pat in composed_patterns for p in _expand_glob(pat)]
    if not expanded_paths:
        expanded_paths = composed_patterns

    # Preserve order while deduplicating.
    seen: set[str] = set()
    new_paths = [p for p in expanded_paths if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    # Compute a new deterministic cache hash over the composed paths + query.
    raw_key = ":".join(sorted(new_paths)) + ":" + dialect + ":" + sql_fragment
    hash_hex = hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    new_output = os.path.join(
        os.path.expanduser("~"), ".cache", "mlody", "derived", hash_hex + ".parquet"
    )

    return Struct(
        kind="location",
        type="derived",
        name="derived",
        abstract=False,
        _root_kind="derived",
        attributes={
            "source_ref": source_ref,
            "source_paths": new_paths,
            "sql_fragment": sql_fragment,
            "dialect": dialect,
            "output_path": new_output,
        },
    )


register_location_composer("derived", _derived_compose)
