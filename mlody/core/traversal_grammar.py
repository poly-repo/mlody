"""Typed AST for mlody traversal path expressions.

Defines the path-segment kinds, ``PathExpression``, ``TraversalParseError``,
and the authoritative ``TRAVERSAL_GRAMMAR_EBNF`` constant.

This module has **zero mandatory runtime dependencies** outside the Python
standard library (design D-1, CON-002).

Design decisions:
- D-2: Frozen dataclasses (not NamedTuple) for hashability, isinstance dispatch,
  and readable repr.
- PathSegment is a plain base class; the sealed-union constraint is enforced by
  convention and the spec, not by Python's type system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, overload

# ---------------------------------------------------------------------------
# EBNF grammar constant  (task 1.4)
# ---------------------------------------------------------------------------

TRAVERSAL_GRAMMAR_EBNF: str = """\
traversal_expr  ::= segment*
segment         ::= field_seg | bracket_seg | recursive_seg
field_seg       ::= "." IDENT
bracket_seg     ::= "[" ( sql_seg | mlody_seg | slice_seg | INT | STR | "*" ) "]"
sql_seg         ::= "@sql" WS* SQL_BODY
mlody_seg       ::= "@mlody" WS* STARLARK_EXPR
slice_seg       ::= INT? ":" INT? ( ":" INT? )?
recursive_seg   ::= ".."
IDENT           ::= [a-zA-Z_][a-zA-Z0-9_]*
INT             ::= "-"? [0-9]+
STR             ::= '"' [^"]* '"'
SQL_BODY        ::= any characters (nested "[]" balanced) up to the closing "]"
STARLARK_EXPR   ::= a Starlark expression (nested brackets / strings balanced)
WS              ::= " " | "\\t"
"""


# ---------------------------------------------------------------------------
# TraversalParseError  (task 1.1)
# ---------------------------------------------------------------------------


class TraversalParseError(Exception):
    """Raised by the parser for any syntax error in a traversal expression.

    Carries structured fields so callers can display or log precise diagnostics:
    ``input_expr`` is the full input string, ``position`` is the character index
    of the first error, and the message describes what was expected or found.
    """

    def __init__(self, message: str, input_expr: str, position: int) -> None:
        self.message = message
        self.input_expr = input_expr
        self.position = position
        super().__init__(message)

    def __str__(self) -> str:
        return (
            f"TraversalParseError at position {self.position} "
            f"in {self.input_expr!r}: {self.message}"
        )


# ---------------------------------------------------------------------------
# PathSegment base and concrete types  (task 1.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathSegment:
    """Abstract base for all path segment kinds.

    Concrete subclasses: ``FieldSegment``, ``IndexSegment``, ``KeySegment``,
    ``WildcardSegment``, ``RecursiveDescentSegment``, ``SqlSegment``,
    ``MlodySegment``, and ``SliceSegment``.  Each implements
    ``__str__`` returning the canonical serialisation for round-trip fidelity.
    """


@dataclass(frozen=True)
class FieldSegment(PathSegment):
    """Dot-notation attribute access: ``.fieldname``."""

    name: str

    def __str__(self) -> str:
        return f".{self.name}"


@dataclass(frozen=True)
class IndexSegment(PathSegment):
    """Zero-based numeric index: ``[n]`` (negative indices follow Python convention)."""

    index: int

    def __str__(self) -> str:
        return f"[{self.index}]"


@dataclass(frozen=True)
class KeySegment(PathSegment):
    """Map key access: ``["key"]``."""

    key: str

    def __str__(self) -> str:
        return f'["{self.key}"]'


@dataclass(frozen=True)
class WildcardSegment(PathSegment):
    """Matches all immediate children at the current level: ``[*]``."""

    def __str__(self) -> str:
        return "[*]"


@dataclass(frozen=True)
class RecursiveDescentSegment(PathSegment):
    """Matches all descendants at any depth: ``..``."""

    def __str__(self) -> str:
        return ".."


@dataclass(frozen=True)
class SqlSegment(PathSegment):
    """SQL query segment: ``[@sql <query>]``.

    The ``query`` field holds the SQL string as supplied by the caller (after
    stripping surrounding whitespace).  The ``@sql`` keyword itself is not
    included.  Nested square brackets inside the SQL body are preserved.
    """

    query: str

    def __str__(self) -> str:
        return f"[@sql {self.query}]"


@dataclass(frozen=True)
class MlodySegment(PathSegment):
    """Entity-filter segment: ``[@mlody <expr>]``.

    The ``query`` field stores the Starlark expression body exactly as supplied
    by the caller, minus surrounding whitespace and without the ``@mlody``
    keyword itself.
    """

    query: str

    def __str__(self) -> str:
        return f"[@mlody {self.query}]"


@dataclass(frozen=True)
class SliceSegment(PathSegment):
    """Python-style slice of a sequence: ``[start:stop]``, ``[start:]``, ``[:stop]``, ``[start:stop:step]``.

    Any of ``start``, ``stop``, ``step`` may be ``None`` (omitted), matching
    Python's ``slice(start, stop, step)`` semantics.
    """

    start: int | None
    stop: int | None
    step: int | None = None

    def __str__(self) -> str:
        parts = [
            "" if self.start is None else str(self.start),
            "" if self.stop is None else str(self.stop),
        ]
        base = ":".join(parts)
        if self.step is not None:
            base += f":{self.step}"
        return f"[{base}]"


# ---------------------------------------------------------------------------
# PathExpression  (task 1.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathExpression:
    """An ordered sequence of ``PathSegment`` objects representing a traversal path.

    Implements ``__str__`` (canonical serialisation), ``__len__``, ``__iter__``,
    and ``__getitem__`` for ergonomic use.  Is hashable (frozen dataclass over a
    hashable tuple) — usable as a dict key.
    """

    segments: tuple[PathSegment, ...]

    def __str__(self) -> str:
        return "".join(str(s) for s in self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def __iter__(self) -> Iterator[PathSegment]:
        return iter(self.segments)

    @overload
    def __getitem__(self, index: int) -> PathSegment: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[PathSegment, ...]: ...

    def __getitem__(self, index: int | slice) -> PathSegment | tuple[PathSegment, ...]:
        return self.segments[index]
