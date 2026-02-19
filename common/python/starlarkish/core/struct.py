"""Immutable value-typed Struct for the Starlarkish sandbox.

``Struct`` is modelled on Starlark's ``struct`` type: dot-accessible and
immutable.

Choosing between ``Struct`` and ``struct()``:

* Use the ``struct(**kwargs)`` factory in scripts and tests.  It coerces
  nested ``dict`` values into nested ``Struct`` instances (Starlark deviation,
  see below).
* Use ``Struct(**kwargs)`` only when you explicitly do *not* want
  dict-to-Struct coercion.

Starlark deviation:
  ``struct(a={...})`` wraps the nested dict as another ``Struct``.
  Standard Starlark leaves nested dicts as plain dicts.  Lists and tuples
  are also walked for dict coercion.
"""
from typing import Any
from types import MappingProxyType


class Struct:
    __slots__ = ("_fields",)
    _fields: MappingProxyType[str, Any]

    def __init__(self, **kwargs: Any):
        # MappingProxyType wraps kwargs at the C level; any mutation attempt raises
        # TypeError — immutability is a data-structure guarantee, not just convention.
        object.__setattr__(self, "_fields", MappingProxyType(kwargs))

    def __getattr__(self, name: str) -> Any:
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("Struct is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Struct):
            return NotImplemented  # pyright: ignore[reportReturnType]
        return self._fields == other._fields

    def __hash__(self) -> int:
        # Raises TypeError for unhashable field values — surfaces the problem at
        # call time rather than silently returning a meaningless id()-based hash.
        return hash(tuple(sorted(self._fields.items())))

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation, recursing into nested Structs.

        Tuple values are coerced to lists in the output so that the result is
        uniformly list-based and JSON-serialisable without further conversion.
        """
        def conv(x: Any) -> Any:
            if isinstance(x, Struct):
                return x.to_dict()
            if isinstance(x, dict):
                return {k: conv(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [conv(v) for v in x]
            return x
        return {k: conv(v) for k, v in self._fields.items()}

    def as_mapping(self) -> MappingProxyType[str, Any]:
        """Read-only view of the backing dict.

        The view is shallow: nested mutable values (e.g. a list stored in a
        field) are not deep-frozen and remain mutable.
        """
        return self._fields

    def __repr__(self) -> str:
        items = ", ".join(f"{k}={v!r}" for k, v in self._fields.items())
        return f"struct({items})"

    # --- Pickle support: simple, reliable ---
    def __getstate__(self) -> dict[str, Any]:
        # MappingProxyType is not directly picklable; convert to a plain dict.
        return dict(self._fields)

    def __setstate__(self, state: dict[str, Any]) -> None:
        # Reconstruct from the pickled plain dict, re-wrapping as proxy.
        object.__setattr__(self, "_fields", MappingProxyType(state))


# factory helper (keeps API similar to Starlark struct())
def struct(**kwargs: Any) -> Struct:
    # Coerce nested dicts -> Struct for nicer nesting semantics.
    # This differs from standard Starlark, which would leave nested dicts as-is.
    def maybe_wrap(x: Any) -> Any:
        if isinstance(x, dict):
            return struct(**x)
        if isinstance(x, (list, tuple)):
            return [maybe_wrap(v) for v in x]
        return x
    wrapped = {k: maybe_wrap(v) for k, v in kwargs.items()}
    return Struct(**wrapped)
