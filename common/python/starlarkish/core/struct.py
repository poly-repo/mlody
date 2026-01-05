from typing import Any, Dict
from types import MappingProxyType
#import pickle

class Struct:
    __slots__ = ("_fields",)
    _fields: Dict[str, Any]

    def __init__(self, **kwargs: Any):
        # Store a plain dict internally; we expose a read-only view if needed.
        object.__setattr__(self, "_fields", dict(kwargs))

    def __getattr__(self, name: str) -> Any:
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("Struct is immutable")

    def to_dict(self) -> Dict[str, Any]:
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
        """Read-only view of the backing dict; avoids exposing mutable dict directly."""
        return MappingProxyType(self._fields)

    def __repr__(self) -> str:
        items = ", ".join(f"{k}={v!r}" for k, v in self._fields.items())
        return f"struct({items})"

    # --- Pickle support: simple, reliable ---
    def __getstate__(self) -> Dict[str, Any]:
        # Return a plain serializable representation (the inner dict)
        # This will be pickled and passed to __setstate__ on unpickle.
        return self._fields

    def __setstate__(self, state: Dict[str, Any]) -> None:
        # Reconstruct from the state (a dict)
        object.__setattr__(self, "_fields", dict(state))

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


# def struct(**kwargs):
#     """
#     Mimic Starlark's struct:
#       - Dot-accessible attributes
#       - Immutable after creation
#       - Can be created dynamically from kwargs
#     """
#     class Struct:
#         __slots__ = kwargs.keys()  # prevent dynamic attributes

#         def __init__(self, **inner_kwargs):
#             for k, v in inner_kwargs.items():
#                 object.__setattr__(self, k, v)

#         def __setattr__(self, key, value):
#             raise AttributeError("Cannot modify fields of a struct")

#         def __repr__(self):
#             fields = ", ".join(f"{k} = {getattr(self, k)!r}" for k in self.__slots__)
#             return f"struct({fields})"

#     return Struct(**kwargs)
