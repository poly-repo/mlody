"""Pure structural unification for mm patterns.

This module is the algorithmic heart of ``mm.unify``.  It is stateless and has
no dependency on the evaluator or any sandbox globals — it accepts plain Python
objects and returns plain Python dicts (or ``None`` on failure).

Design notes (see openspec/changes/mm-pattern-unification/design.md):
- Decision 1: This is a separate module from ``multimethod.py`` because
  dispatch scoring (``int | None``) and unification (``dict | None``) are
  distinct algorithms that thread different state.
- Decision 3: Failure is represented as ``None`` because it is falsy in both
  Python and Starlark, making ``if mm.unify(a, b):`` work idiomatically.
"""

from __future__ import annotations


def unify(
    a: object,
    b: object,
    bindings: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Structurally unify ``a`` and ``b`` under ``bindings``.

    Returns a binding dict on success.  An empty dict ``{}`` means success
    with no free variables.  Returns ``None`` on any failure: kind mismatch,
    literal inequality, or occurrence-check conflict.

    Never raises for semantic mismatches; only truly unexpected inputs (e.g. a
    corrupted pattern struct missing required fields) can cause exceptions.

    Parameters
    ----------
    a:
        First value — may be a pattern struct or a bare literal.
    b:
        Second value — may be a pattern struct or a bare literal.
    bindings:
        Accumulated variable bindings from an outer call.  ``None`` is treated
        as an empty dict; the caller's dict is never mutated.
    """
    acc = dict(bindings) if bindings is not None else {}
    return _unify(a, b, acc)


# ---------------------------------------------------------------------------
# Internal recursive implementation
# ---------------------------------------------------------------------------


def _unify(
    a: object,
    b: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Recursive unification worker.  ``bindings`` must not be mutated."""

    # 1. Both mm.ANY — succeed with no new bindings.
    if _is_struct_kind(a, "mm_any") and _is_struct_kind(b, "mm_any"):
        return bindings

    # 2. One side is mm.ANY — discard (nameless wildcard).
    if _is_struct_kind(a, "mm_any") or _is_struct_kind(b, "mm_any"):
        return bindings

    # 3. mm.var — bind the var_name to the other side.
    if _is_struct_kind(a, "mm_var_pattern"):
        return _bind(getattr(a, "var_name", ""), b, bindings)
    if _is_struct_kind(b, "mm_var_pattern"):
        return _bind(getattr(b, "var_name", ""), a, bindings)

    # 4. Both bare literals — equality check.
    if not _is_mm_pattern(a) and not _is_mm_pattern(b):
        return bindings if a == b else None

    # 5. mm.literal — compare .value to the other side.
    if _is_struct_kind(a, "mm_literal_pattern"):
        value = getattr(a, "value", None)
        return bindings if value == b else None
    if _is_struct_kind(b, "mm_literal_pattern"):
        value = getattr(b, "value", None)
        return bindings if value == a else None

    # 6. mm.or_ — try each branch left-to-right, return first success.
    if _is_struct_kind(a, "mm_or_pattern"):
        return _unify_or(a, b, bindings)
    if _is_struct_kind(b, "mm_or_pattern"):
        return _unify_or(b, a, bindings)

    # 7. mm_scalar_pattern — check type_name/name field against the arg.
    if _is_struct_kind(a, "mm_scalar_pattern"):
        return _unify_scalar(a, b, bindings)
    if _is_struct_kind(b, "mm_scalar_pattern"):
        return _unify_scalar(b, a, bindings)

    # 8. mm_repr_pattern — check repr_name/name field against the arg.
    if _is_struct_kind(a, "mm_repr_pattern"):
        return _unify_repr(a, b, bindings)
    if _is_struct_kind(b, "mm_repr_pattern"):
        return _unify_repr(b, a, bindings)

    # 9. mm_entity_pattern — check entity_kind + entity_name, recurse into fields.
    # When both sides are entity patterns (pattern-vs-pattern unification),
    # check that they describe the same entity then unify field sub-patterns
    # pairwise, threading bindings across all fields.
    if _is_struct_kind(a, "mm_entity_pattern") and _is_struct_kind(b, "mm_entity_pattern"):
        return _unify_two_entity_patterns(a, b, bindings)
    if _is_struct_kind(a, "mm_entity_pattern"):
        return _unify_entity(a, b, bindings)
    if _is_struct_kind(b, "mm_entity_pattern"):
        return _unify_entity(b, a, bindings)

    # 10. Fallback — structural equality.
    return bindings if a == b else None


# ---------------------------------------------------------------------------
# Per-pattern-kind helpers
# ---------------------------------------------------------------------------


def _unify_or(
    or_pattern: object,
    arg: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Left-to-right first match for mm_or_pattern.

    For unification (as opposed to dispatch), committed-choice semantics are
    correct: stop at the first branch that succeeds and return that branch's
    bindings.  Returning the first-match bindings rather than the maximum-score
    bindings preserves deterministic left-to-right evaluation order.
    """
    branches: list[object] = list(getattr(or_pattern, "patterns", []))
    for branch in branches:
        result = _unify(branch, arg, bindings)
        if result is not None:
            return result
    return None


def _unify_scalar(
    pattern: object,
    arg: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Unify mm_scalar_pattern: check that arg is a type struct with a matching name.

    Mirrors ``MmScalarPattern.score`` in ``multimethod.py`` for the name-lookup
    logic (type_name first, then name fallback), but produces a dict instead of
    an int.
    """
    if getattr(arg, "kind", None) != "type":
        return None
    arg_type_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
    pattern_type_name = getattr(pattern, "type_name", None)
    if arg_type_name == pattern_type_name:
        return bindings
    return None


def _unify_repr(
    pattern: object,
    arg: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Unify mm_repr_pattern: check that arg is a representation struct with a matching name.

    Mirrors ``MmReprPattern.score`` for the name-lookup logic.
    """
    if getattr(arg, "kind", None) != "representation":
        return None
    # Real mlody structs carry repr_name; test stubs may carry only name.
    arg_repr_name = getattr(arg, "repr_name", None) or getattr(arg, "name", None)
    pattern_repr_name = getattr(pattern, "repr_name", None)
    if arg_repr_name == pattern_repr_name:
        return bindings
    return None


def _unify_entity(
    pattern: object,
    arg: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Unify mm_entity_pattern: check entity_kind + entity_name, recurse into fields.

    Field patterns are matched in iteration order.  Bindings are threaded
    accumulator-style: each field's result becomes the next field's starting
    bindings, so occurrence checks cover the whole pattern.
    """
    entity_kind: str = getattr(pattern, "entity_kind", "")
    entity_name: str = getattr(pattern, "entity_name", "")

    if getattr(arg, "kind", None) != entity_kind:
        return None

    # Type structs use type_name; other entities use name.
    arg_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
    if arg_name != entity_name:
        return None

    acc = bindings
    field_patterns: object = getattr(pattern, "field_patterns", None) or {}
    # field_patterns may be a plain dict or a Struct (dict-like via _fields).
    items: list[tuple[str, object]] = _dict_items(field_patterns)
    for field, sub_pattern in items:
        field_val = getattr(arg, field, None)
        if field_val is None:
            _attrs = getattr(arg, "attributes", None)
            if isinstance(_attrs, dict):
                field_val = _attrs.get(field)
        # Absent (None) fields with a capture/discard pattern are silently
        # skipped — no noise bindings like {min_length: None}.
        if field_val is None and (
            _is_struct_kind(sub_pattern, "mm_var_pattern")
            or _is_struct_kind(sub_pattern, "mm_any")
        ):
            continue
        # mm.ANY in entity field context: capture present values under the
        # field name (equivalent to an implicit var).  This differs from
        # top-level mm.ANY which truly discards; here the user wrote
        # mm.ANY to say "any value, capture it by field name."
        if _is_struct_kind(sub_pattern, "mm_any"):
            result = _bind(field, field_val, acc)
            if result is None:
                return None
            acc = result
            continue
        result = _unify(sub_pattern, field_val, acc)
        if result is None:
            return None
        acc = result
    return acc


def _unify_two_entity_patterns(
    a: object,
    b: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Unify two mm_entity_pattern structs (pattern-vs-pattern).

    Checks that both patterns describe the same entity (entity_kind +
    entity_name), then recursively unifies corresponding field sub-patterns,
    threading bindings across all shared fields.  Fields present in one
    pattern but absent in the other are skipped.
    """
    if getattr(a, "entity_kind", "") != getattr(b, "entity_kind", ""):
        return None
    if getattr(a, "entity_name", "") != getattr(b, "entity_name", ""):
        return None

    b_fields: dict[str, object] = dict(_dict_items(getattr(b, "field_patterns", None) or {}))
    acc = bindings
    for field, a_sub in _dict_items(getattr(a, "field_patterns", None) or {}):
        b_sub = b_fields.get(field)
        if b_sub is None:
            continue
        result = _unify(a_sub, b_sub, acc)
        if result is None:
            return None
        acc = result
    return acc


# ---------------------------------------------------------------------------
# Binding helpers
# ---------------------------------------------------------------------------


def _bind(
    name: str,
    value: object,
    bindings: dict[str, object],
) -> dict[str, object] | None:
    """Attempt to add or verify ``name → value`` in ``bindings``.

    - If ``name`` is not in ``bindings``: return a new dict with the binding added.
    - If ``name`` is in ``bindings`` and ``bindings[name] == value``: return
      ``bindings`` unchanged (idempotent).
    - If ``name`` is in ``bindings`` and ``bindings[name] != value``: return
      ``None`` (occurrence-check conflict).

    Never mutates the input dict.
    """
    if name not in bindings:
        return {**bindings, name: value}
    if bindings[name] == value:
        return bindings
    return None


def _merge_bindings(
    acc: dict[str, object],
    new_bindings: dict[str, object],
) -> dict[str, object] | None:
    """Merge ``new_bindings`` into ``acc``, enforcing occurrence check on every key.

    Returns the merged dict on success, ``None`` if any key conflicts.
    Never mutates ``acc``.
    """
    result = acc
    for k, v in new_bindings.items():
        updated = _bind(k, v, result)
        if updated is None:
            return None
        result = updated
    return result


# ---------------------------------------------------------------------------
# Utility predicates
# ---------------------------------------------------------------------------


def _is_mm_pattern(obj: object) -> bool:
    """Return True if ``obj`` is an mm pattern (has a ``kind`` starting with ``mm_``)."""
    kind = getattr(obj, "kind", None)
    return isinstance(kind, str) and kind.startswith("mm_")


def _is_struct_kind(obj: object, kind: str) -> bool:
    """Return True if ``obj`` is a Struct-like object with the given ``kind`` field."""
    return getattr(obj, "kind", None) == kind


def _dict_items(mapping: object) -> list[tuple[str, object]]:
    """Extract (key, value) pairs from a plain dict or a Struct wrapping a dict."""
    if isinstance(mapping, dict):
        return list(mapping.items())
    # Struct stores fields in _fields (a MappingProxyType).
    fields = getattr(mapping, "_fields", None)
    if fields is not None:
        return list(fields.items())
    # Last resort: try items() (covers other mapping-like objects).
    items_fn = getattr(mapping, "items", None)
    if callable(items_fn):
        return list(items_fn())
    return []
