"""Pure dispatch algorithm for the mm multimethod system.

This module is a stateless algorithm library. It does not own any global
method registry; the caller passes the method list at dispatch time.
This makes the module trivially testable and avoids process-global side
effects that would make test isolation fragile.

The registry lives on the Evaluator instance (self._method_registry) and
is threaded in via closures defined in mm.mlody.
"""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.python.starlarkish.core.struct import Struct


class DispatchError(Exception):
    """Raised when no registered method matches a dispatch call.

    The message names the generic, the arguments, and lists every registered
    method's patterns so authors can diagnose dispatch gaps without reading
    source code.
    """


def _match_score(pattern: object, arg: object) -> int | None:
    """Return a specificity score for matching pattern against arg.

    Returns None if the pattern does not match. Score values:
    - 1   : mm.ANY wildcard (least specific)
    - 1   : mm.posix with ** multi-segment wildcard
    - 2   : exact string match
    - 2   : mm.posix with * single-segment wildcard only
    - 3   : mm.T scalar type pattern
    - 3   : mm.json (or other bare repr constant) match
    - 3   : mm.posix exact path (no wildcards)
    - 3 + sum(subscores) : mm.value composite pattern
    - 3 + subscore       : mm.vector pattern

    Field fallbacks for real mlody structs vs. test stubs:
    - mm.T / mm.vector : reads `arg.type_name`, falls back to `arg.name`
    - mm.json (repr)   : reads `arg.repr_name`, falls back to `arg.name`
    - mm.posix (loc)   : reads `arg.location_type`, falls back to `arg.type`
    """
    # --- mm.ANY ---
    if _is_struct_kind(pattern, "mm_any"):
        return 1

    # --- exact string ---
    if isinstance(pattern, str):
        return 2 if arg == pattern else None

    # --- mm.T scalar type pattern ---
    if _is_struct_kind(pattern, "mm_scalar_pattern"):
        if _is_struct_kind(arg, "type"):
            arg_type_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
            if arg_type_name == getattr(pattern, "type_name", None):
                return 3
        return None

    # --- mm.json / bare repr constant ---
    if _is_struct_kind(pattern, "mm_repr_pattern"):
        if _is_struct_kind(arg, "representation"):
            # Real mlody representation structs carry `name`; stub/test structs may
            # carry `repr_name`. Fall back to `name` so both work.
            arg_repr_name = getattr(arg, "repr_name", None) or getattr(arg, "name", None)
            if arg_repr_name == getattr(pattern, "repr_name", None):
                return 3
        return None

    # --- mm.posix location pattern ---
    if _is_struct_kind(pattern, "mm_posix_pattern"):
        if not _is_struct_kind(arg, "location"):
            return None
        # Real mlody location structs carry `type`; stub/test structs may carry
        # `location_type`. Fall back to `type` so both work.
        loc_kind = getattr(arg, "location_type", None) or getattr(arg, "type", None)
        if loc_kind != "posix":
            return None
        path = getattr(arg, "path", "")
        path_pattern: str = getattr(pattern, "path_pattern", "")
        return _posix_match_score(path_pattern, path)

    # --- mm.vector pattern ---
    if _is_struct_kind(pattern, "mm_vector_pattern"):
        if not _is_struct_kind(arg, "type"):
            return None
        arg_type_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
        if arg_type_name != "vector":
            return None
        element_type = getattr(arg, "element_type", None)
        # Real mlody vector types produced by _make_factory store element_type
        # inside arg.attributes, not as a direct field.
        if element_type is None:
            _attrs = getattr(arg, "attributes", None)
            if isinstance(_attrs, dict):
                element_type = _attrs.get("element_type")
        element_pattern = getattr(pattern, "element_type", None)
        if element_pattern is None:
            return 4  # 3 + 1 (default mm.ANY)
        sub = _match_score(element_pattern, element_type) if element_type is not None else None
        if sub is None:
            return None
        return 3 + sub

    # --- mm.value composite pattern ---
    if _is_struct_kind(pattern, "mm_value_pattern"):
        if not _is_struct_kind(arg, "value"):
            return None
        raw_fields: Any = getattr(pattern, "fields", {}) or {}
        # When mm.value() is called from Starlark, the struct() factory coerces
        # the **kwargs dict into a Struct.  Normalise both cases to a mapping.
        fields: Any = raw_fields.as_mapping() if hasattr(raw_fields, "as_mapping") else raw_fields
        if not fields:
            return 3
        total = 0
        for field_name, field_pattern in fields.items():
            field_val = getattr(arg, field_name, None)
            sub = _match_score(field_pattern, field_val)
            if sub is None:
                return None
            total += sub
        return 3 + total

    # --- mm.source_range ---
    if _is_struct_kind(pattern, "mm_source_range_pattern"):
        if _is_struct_kind(arg, "mlody-source-range"):
            return 3
        return None

    return None


def _is_struct_kind(obj: object, kind: str) -> bool:
    """Return True if obj is a Struct-like object with the given kind field."""
    k = getattr(obj, "kind", None)
    return k == kind


def _posix_match_score(path_pattern: str, path: str) -> int | None:
    """Match a POSIX glob pattern against a path, returning a specificity score.

    Score semantics:
    - 3 : no wildcards (exact path)
    - 2 : only single-segment wildcards (* without **)
    - 1 : multi-segment wildcard (**) present

    Uses fnmatch-style glob where ** crosses segment boundaries and *
    matches within a single segment.
    """
    if "**" in path_pattern:
        score = 1
        regex = _glob_to_regex(path_pattern)
        return score if re.fullmatch(regex, path) else None
    elif "*" in path_pattern or "?" in path_pattern:
        score = 2
        # Single-segment wildcard: * must not cross /
        regex = _glob_to_regex_single_segment(path_pattern)
        return score if re.fullmatch(regex, path) else None
    else:
        # Exact path match
        return 3 if path == path_pattern else None


def _glob_to_regex(pattern: str) -> str:
    """Convert a ** glob pattern to a regex (** crosses segment boundaries)."""
    parts = pattern.split("**")
    regex_parts = []
    for i, part in enumerate(parts):
        # Escape the non-wildcard part, then handle single *
        escaped = re.escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        regex_parts.append(escaped)
        if i < len(parts) - 1:
            regex_parts.append(".*")
    return "".join(regex_parts)


def _glob_to_regex_single_segment(pattern: str) -> str:
    """Convert a single-segment glob to a regex (* does not cross /)."""
    return re.escape(pattern).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")


def dispatch(
    generic_name: str,
    args: tuple[object, ...],
    methods: list[Any],  # list of Struct-like method objects
) -> object:
    """Score all registered methods, select the winner, build ctx, and call body.

    Raises DispatchError when no method matches, including when the methods
    list is empty. The first-registered method wins on score ties.

    The ctx struct passed to body contains:
    - ctx.generic: the generic name string
    - ctx.captures: a dict mapping string position index to the matched argument
    """
    # Avoid importing Struct at module level to keep this module lightweight;
    # we only need it for ctx construction.
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    best_score: int | None = None
    best_method: Any = None

    for method in methods:
        patterns: list[object] = list(getattr(method, "patterns", []))
        if len(patterns) != len(args):
            # Arity mismatch at dispatch time should not occur if arity was
            # enforced at registration, but skip gracefully rather than crash.
            continue
        total = 0
        matched = True
        for pattern, arg in zip(patterns, args):
            score = _match_score(pattern, arg)
            if score is None:
                matched = False
                break
            total += score
        if not matched:
            continue
        # First-registered wins on tie (lower index in list = registered first)
        if best_score is None or total > best_score:
            best_score = total
            best_method = method

    if best_method is None:
        method_summary = _format_methods(methods)
        raise DispatchError(
            f"No method matched generic {generic_name!r} for args {args!r}.\n"
            f"Registered methods:\n{method_summary}"
        )

    captures = {str(i): arg for i, arg in enumerate(args)}
    ctx = Struct(
        kind="mm_dispatch_ctx",
        generic=generic_name,
        captures=captures,
    )
    body = getattr(best_method, "body", None)
    return body(ctx, *args)  # type: ignore[operator]


def _format_methods(methods: list[Any]) -> str:
    """Format a list of methods for inclusion in DispatchError messages."""
    if not methods:
        return "  (none)"
    lines = []
    for i, m in enumerate(methods):
        patterns = list(getattr(m, "patterns", []))
        lines.append(f"  [{i}] patterns={patterns!r}")
    return "\n".join(lines)
