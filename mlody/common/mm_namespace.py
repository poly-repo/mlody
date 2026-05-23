"""Python-side implementation of the mlody `mm` dispatch namespace.

``MmNamespace`` replaces the Starlark ``mm = struct(...)`` assembly that was
previously assembled at the end of ``mm.mlody``.  Using a Python class (rather
than an immutable Starlark Struct) allows Phase-2 entity definitions to register
new pattern constructors on the shared singleton — something impossible with the
immutable Struct approach.

Design notes (see openspec/changes/mm-pattern-unification/design.md §Decision 2):
- Fixed attrs are set directly on the instance in ``__init__``.
- ``_dynamic`` holds auto-registered constructors added by ``_register``.
- ``__getattr__`` is the fallback lookup path: raises ``AttributeError`` for
  anything not in ``_dynamic``, which Starlark surfaces as a normal attribute
  error.
"""

from __future__ import annotations


class MmNamespace:
    """The ``mm`` sandbox global: a mutable namespace for multimethod dispatch.

    Fixed attributes (constants and factory callables from ``mm.mlody``) are
    set directly on the instance so that normal attribute access works without
    going through ``__getattr__``.  Auto-registered constructors (added in Phase
    2 by ``_register``) are stored in ``_dynamic`` and looked up by
    ``__getattr__``.
    """

    def __init__(self, **fixed_attrs: object) -> None:
        # Store _dynamic before any setattr so __getattr__ never recurses.
        object.__setattr__(self, "_dynamic", {})
        for name, value in fixed_attrs.items():
            object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> object:
        # Only called when normal attribute lookup fails (i.e. the name is not
        # set as an instance/class attribute).  Check _dynamic before giving up.
        dynamic: dict[str, object] = object.__getattribute__(self, "_dynamic")
        if name in dynamic:
            return dynamic[name]
        raise AttributeError(
            f"mm has no attribute {name!r}. "
            "If this is an entity pattern, ensure the entity is defined before "
            "accessing mm.<name>."
        )

    def _register(self, attr_name: str, constructor: object) -> None:
        """Register a dynamic attribute (auto-generated entity pattern constructor).

        Raises ``ValueError`` if ``attr_name`` is already registered in ``_dynamic``
        or already set as a fixed instance attribute.  Collision detection covers
        both dynamic-dynamic and fixed-dynamic conflicts.
        """
        dynamic: dict[str, object] = object.__getattribute__(self, "_dynamic")
        if attr_name in dynamic:
            raise ValueError(
                f"mm: attribute {attr_name!r} is already registered as a dynamic "
                "constructor. Each entity name must be unique after mangling."
            )
        try:
            # If the name resolves via normal attribute lookup it is a fixed attr.
            object.__getattribute__(self, attr_name)
            raise ValueError(
                f"mm: attribute {attr_name!r} conflicts with a fixed mm attribute. "
                "Choose a different entity name."
            )
        except AttributeError:
            pass
        dynamic[attr_name] = constructor

    def var(self, name: str) -> object:
        """Return a ``mm_var_pattern`` struct capturing the matched value.

        ``name`` must be a non-empty string; it identifies the binding in the
        unification result dict.  ``mm.var("")`` is rejected because an unnamed
        variable is meaningless and most likely a caller bug.
        """
        if not name:
            raise ValueError("mm.var: name must be a non-empty string")
        from common.python.starlarkish.core.struct import Struct

        return Struct(kind="mm_var_pattern", var_name=name)

    def literal(self, v: object) -> object:
        """Return a ``mm_literal_pattern`` struct that matches only the value ``v``.

        Any value is accepted — there is no type restriction.
        """
        from common.python.starlarkish.core.struct import Struct

        return Struct(kind="mm_literal_pattern", value=v)

    def or_(self, *patterns: object) -> object:
        """Return a ``mm_or_pattern`` struct representing a disjunction of patterns.

        At least one pattern is required; zero arguments raises ``ValueError``
        because an empty disjunction has no defined semantics.
        """
        if not patterns:
            raise ValueError(
                "mm.or_: at least one pattern argument is required"
            )
        from common.python.starlarkish.core.struct import Struct

        return Struct(kind="mm_or_pattern", patterns=list(patterns))

    def unify(self, a: object, b: object) -> dict[str, object] | None:
        """Structurally unify ``a`` and ``b``, returning bindings or None.

        Delegates to ``mlody.core.unification.unify``.  Returns a binding dict
        on success (``{}`` means success with no free variables) and ``None`` on
        any mismatch — never raises for semantic failures.
        """
        from mlody.core.unification import unify as _unify  # noqa: PLC0415

        return _unify(a, b)


def _extract_attr_names(attrs: object) -> list[str]:
    """Extract declared attribute names from a rule() attrs schema.

    Filters out private attrs (those starting with '_') since they are
    infrastructure, not entity-specific fields.
    """
    if attrs is None:
        return []
    if isinstance(attrs, dict):
        return [k for k in attrs if not str(k).startswith("_")]
    # Struct: use as_mapping() if available, else fall back to _fields.
    mapping = getattr(attrs, "as_mapping", None)
    if callable(mapping):
        return [k for k in mapping() if not str(k).startswith("_")]
    fields = getattr(attrs, "_fields", None)
    if fields is not None:
        return [k for k in fields if not str(k).startswith("_")]
    return []


def _attr_is_mandatory(attr_def: object) -> bool:
    """Return True if an attr definition declares mandatory=True.

    Handles both plain dicts and Structs (rule.mlody coerces dicts to Structs).
    Defaults to True when the flag cannot be determined.
    """
    if isinstance(attr_def, dict):
        metadata = attr_def.get("metadata")
        if isinstance(metadata, dict):
            return bool(metadata.get("mandatory", True))
        return True
    metadata = getattr(attr_def, "metadata", None)
    if metadata is not None:
        m = getattr(metadata, "mandatory", None)
        if m is not None:
            return bool(m)
        if isinstance(metadata, dict):
            return bool(metadata.get("mandatory", True))
    m = getattr(attr_def, "mandatory", None)
    if m is not None:
        return bool(m)
    return True


def _extract_mandatory_attr_names(attrs: object) -> set[str]:
    """Return the set of non-private attr names that declare mandatory=True."""
    if attrs is None:
        return set()
    if isinstance(attrs, dict):
        return {k for k, v in attrs.items() if not str(k).startswith("_") and _attr_is_mandatory(v)}
    mapping = getattr(attrs, "as_mapping", None)
    if callable(mapping):
        return {k for k, v in mapping().items() if not str(k).startswith("_") and _attr_is_mandatory(v)}
    fields = getattr(attrs, "_fields", None)
    if fields is not None:
        return {k for k, v in fields.items() if not str(k).startswith("_") and _attr_is_mandatory(v)}
    return set()


def _make_entity_pattern_constructor(
    entity_kind: str,
    entity_name: str,
    attrs: object,
) -> object:
    """Return a callable pattern constructor for an entity defined by rule().

    The constructor accepts **kwargs where each key is an attr name declared in
    ``attrs``.  When called:

    - Explicitly passed fields are used as-is (any pattern type is accepted).
    - Omitted fields become implicit ``mm_var_pattern`` captures keyed by the
      field name.  At unification time, a var bound to ``None`` (absent field)
      is silently skipped, so only actually-set fields appear in the result.
    - A field explicitly set to ``mm.ANY`` (kind="mm_any") is kept as a discard.

    Returns a ``Struct(kind="mm_entity_pattern", entity_kind=...,
    entity_name=..., field_patterns={...})``.
    """
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    attr_names: list[str] = _extract_attr_names(attrs)

    def constructor(**kwargs: object) -> object:
        field_patterns: dict[str, object] = {}
        for name in attr_names:
            if name in kwargs:
                field_patterns[name] = kwargs[name]
            else:
                field_patterns[name] = Struct(kind="mm_var_pattern", var_name=name)
        return Struct(
            kind="mm_entity_pattern",
            entity_kind=entity_kind,
            entity_name=entity_name,
            field_patterns=field_patterns,
        )

    return constructor
