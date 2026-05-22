"""Pure dispatch algorithm for the mm multimethod system.

This module is a stateless algorithm library. It does not own any global
method registry; the caller passes the method list at dispatch time.
This makes the module trivially testable and avoids process-global side
effects that would make test isolation fragile.

The registry lives on the Evaluator instance (self._method_registry) and
is threaded in via closures defined in mm.mlody.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from common.python.starlarkish.core.struct import Struct


class DispatchError(Exception):
    """Raised when no registered method matches a dispatch call.

    The message names the generic, the arguments, and lists every registered
    method's patterns so authors can diagnose dispatch gaps without reading
    source code.
    """


@runtime_checkable
class Pattern(Protocol):
    """Structural protocol for pattern-matching strategy objects.

    Each registered pattern class implements ``score`` as a classmethod
    because it acts as a pure function over the (pattern, arg) pair with
    no per-instance state.
    """

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        """Return a specificity score, or None if the pattern does not match."""
        ...


_PATTERN_REGISTRY: dict[str, type[Pattern]] = {}


def register_pattern(kind: str) -> Callable[[type[Pattern]], type[Pattern]]:
    """Decorator factory that registers a Pattern class under ``kind``.

    The class is inserted into ``_PATTERN_REGISTRY`` and returned unchanged.
    """

    def _decorator(cls: type[Pattern]) -> type[Pattern]:
        _PATTERN_REGISTRY[kind] = cls
        return cls

    return _decorator


# ---------------------------------------------------------------------------
# Pattern class implementations
# ---------------------------------------------------------------------------


@register_pattern("mm_any")
class MmAnyPattern:
    """Matches any argument with score 1 (wildcard, least specific)."""

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        return 1


@register_pattern("mm_scalar_pattern")
class MmScalarPattern:
    """Matches a type struct by its type_name field (score 3)."""

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        arg_type_name: str | None = None
        if _is_struct_kind(arg, "type"):
            arg_type_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
        elif isinstance(arg, str) and arg.startswith(":"):
            # Unresolved label reference stored by _make_factory, e.g. ":celebA-row"
            arg_type_name = arg[1:]
        if arg_type_name == getattr(pattern, "type_name", None):
            return 3
        return None


@register_pattern("mm_repr_pattern")
class MmReprPattern:
    """Matches a representation struct by its repr_name field (score 3)."""

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        if _is_struct_kind(arg, "representation"):
            # Real mlody representation structs carry `name`; stub/test structs may
            # carry `repr_name`. Fall back to `name` so both work.
            arg_repr_name = getattr(arg, "repr_name", None) or getattr(arg, "name", None)
            if arg_repr_name == getattr(pattern, "repr_name", None):
                return 3
        return None


@register_pattern("mm_posix_pattern")
class MmPosixPattern:
    """Matches a POSIX location struct by glob pattern (scores 1/2/3)."""

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
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


@register_pattern("mm_var_pattern")
class MmVarPattern:
    """Matches any argument with score 1 — a named wildcard for unification.

    Dispatch scoring is identical to mm.ANY: score 1 for everything. The var_name
    field is used by the unification engine (task 4) to bind captured values;
    the score method ignores it and accepts unconditionally.
    """

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        return 1


@register_pattern("mm_literal_pattern")
class MmLiteralPattern:
    """Matches an argument equal to pattern.value (score 2).

    Same specificity as an exact string match; works for any Python value type.
    """

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        value = getattr(pattern, "value", None)
        if value == arg:
            return 2
        return None


@register_pattern("mm_or_pattern")
class MmOrPattern:
    """Matches when any branch matches; returns the maximum branch score.

    For dispatch, the maximum score is used rather than first-match so that an
    or-pattern registered alongside a specific method does not artificially
    lower the winner's specificity (design.md Decision 7).
    """

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        branches: list[object] = list(getattr(pattern, "patterns", []))
        best: int | None = None
        for branch in branches:
            sub = _match_score(branch, arg)
            if sub is not None:
                best = sub if best is None else max(best, sub)
        return best


@register_pattern("mm_entity_pattern")
class MmEntityPattern:
    """Matches an entity struct by kind, name, and optional field sub-patterns.

    This is the single handler for all auto-generated entity patterns and
    replaces the hand-written MmVectorPattern, MmValuePattern, and
    MmSourceRangePattern classes removed in task 7.3.

    Scoring:
      1. Check arg.kind == pattern.entity_kind.
      2. If entity_name is non-empty, check arg's name against it (try
         arg.type_name first, then arg.name). When entity_name is empty, the
         check is skipped — this covers kind-level patterns like mm.value()
         that match any entity of the given kind regardless of instance name.
      3. For each (field, sub_pattern) in field_patterns: resolve the field
         value from arg (direct attribute first, then arg.attributes dict as
         a fallback for type structs that store attrs inside attributes).
         Call _match_score; if any sub-score is None, return None.
      4. Return 3 + sum(sub_scores).
    """

    @classmethod
    def score(cls, pattern: object, arg: object) -> int | None:
        entity_kind: str = getattr(pattern, "entity_kind", "")
        entity_name: str = getattr(pattern, "entity_name", "")

        if getattr(arg, "kind", None) != entity_kind:
            return None

        # When entity_name is non-empty, verify the arg's name matches.  An
        # empty entity_name acts as a wildcard: match any entity of this kind
        # (used by mm.value() and mm.source_range which cover all instances).
        if entity_name:
            arg_name = getattr(arg, "type_name", None) or getattr(arg, "name", None)
            if arg_name != entity_name:
                return None

        field_patterns: dict[str, object] = getattr(pattern, "field_patterns", {}) or {}
        total = 0
        for field, sub_pattern in field_patterns.items():
            field_val = getattr(arg, field, None)
            # Real mlody type structs (produced by _make_factory) store attrs
            # inside an 'attributes' dict rather than as direct fields.  Fall
            # back to attributes lookup so that e.g. element_type on a vector
            # type struct is correctly resolved during dispatch.
            if field_val is None:
                _attrs = getattr(arg, "attributes", None)
                if isinstance(_attrs, dict):
                    field_val = _attrs.get(field)
            sub = _match_score(sub_pattern, field_val)
            if sub is None:
                return None
            total += sub

        return 3 + total


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------


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
    - 3 + sum(subscores) : mm_entity_pattern (mm.value, mm.vector, etc.)

    Field fallbacks for real mlody structs vs. test stubs:
    - mm.T       : reads `arg.type_name`, falls back to `arg.name`
    - mm.json    : reads `arg.repr_name`, falls back to `arg.name`
    - mm.posix   : reads `arg.location_type`, falls back to `arg.type`
    - mm.vector  : entity field lookup falls back to `arg.attributes` dict
    """
    # Exact string match is checked before registry lookup: str is not a
    # struct-like object with a `kind` field, so it cannot be registered.
    if isinstance(pattern, str):
        return 2 if arg == pattern else None

    kind: str | None = getattr(pattern, "kind", None)
    if kind is None:
        return None

    cls = _PATTERN_REGISTRY.get(kind)
    if cls is None:
        return None

    return cls.score(pattern, arg)


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
