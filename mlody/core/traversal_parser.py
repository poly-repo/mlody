"""Hand-rolled recursive descent parser for mlody traversal path expressions.

Accepts a UTF-8 string containing a traversal expression (may be empty) and
returns a ``PathExpression`` on success, or raises ``TraversalParseError`` on
any syntax error.

This module has **zero mandatory runtime dependencies** outside the Python
standard library (design D-1, CON-002).

Grammar (see ``TRAVERSAL_GRAMMAR_EBNF`` in ``traversal_grammar``):

    traversal_expr  ::= segment*
    segment         ::= field_seg | bracket_seg | recursive_seg
    field_seg       ::= "." IDENT
    bracket_seg     ::= "[" ( sql_seg | mlody_seg | slice_seg | INT | STR | "*" ) "]"
    recursive_seg   ::= ".."
    IDENT           ::= [a-zA-Z_][a-zA-Z0-9_]*
    INT             ::= "-"? [0-9]+
    STR             ::= '"' [^"]* '"'

The parser is a direct transliteration of the grammar. Parsing is stateless;
the only state is the cursor position (``_pos``) inside the ``_Parser`` helper
class, which is instantiated fresh for each call to ``parse_traversal_expression``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from common.python.starlarkish.core.struct import Struct
from mlody.common.struct import struct_like_to_struct
from mlody.core.traversal_grammar import (
    FieldSegment,
    IndexSegment,
    KeySegment,
    MlodySegment,
    PathExpression,
    PathSegment,
    RecursiveDescentSegment,
    SliceSegment,
    SqlSegment,
    TraversalParseError,
    WildcardSegment,
)


_MLODY_EVAL_GLOBALS: dict[str, object] = {
    "__builtins__": {},
    "False": False,
    "None": None,
    "True": True,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "false": False,
    "float": float,
    "getattr": getattr,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "none": None,
    "range": range,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "true": True,
    "tuple": tuple,
    "type": type,
    "zip": zip,
}
_MLODY_QUERY_WRAP_PREFIX = "_mlody_query = ("
_MLODY_QUERY_WRAP_SUFFIX = ")\n"


def _first_tree_syntax_issue(node: Any) -> Any | None:
    if node.type == "ERROR" or node.is_missing:
        return node
    for child in node.children:
        issue = _first_tree_syntax_issue(child)
        if issue is not None:
            return issue
    return None


@lru_cache(maxsize=1)
def _starlark_language() -> Any:
    import tree_sitter
    import tree_sitter_starlark as _ts_starlark

    return tree_sitter.Language(_ts_starlark.language())


def _validate_mlody_query(query: str, *, expr: str, query_start: int) -> None:
    if not query.strip():
        raise TraversalParseError(
            "empty '[@mlody ...]' expression",
            expr,
            query_start,
        )

    import tree_sitter

    wrapped = f"{_MLODY_QUERY_WRAP_PREFIX}{query}{_MLODY_QUERY_WRAP_SUFFIX}"
    parser = tree_sitter.Parser(_starlark_language())
    tree = parser.parse(wrapped.encode())
    issue = _first_tree_syntax_issue(tree.root_node)
    if issue is None:
        return

    relative_position = max(
        0,
        min(
            len(query),
            int(issue.start_byte) - len(_MLODY_QUERY_WRAP_PREFIX),
        ),
    )
    detail = "syntax error" if issue.type == "ERROR" else f"missing {issue.type}"
    raise TraversalParseError(
        f"invalid @mlody expression: {detail}",
        expr,
        query_start + relative_position,
    )


def _coerce_mlody_result_to_bool(result: object, *, query: str) -> bool:
    if not isinstance(result, bool):
        raise TypeError(
            f"@mlody expression {query!r} must return bool, got {type(result).__name__}.",
        )
    return result


@lru_cache(maxsize=256)
def compile_mlody_query_predicate(query: str) -> Callable[[Struct], bool]:
    try:
        globals_env = dict(_MLODY_EVAL_GLOBALS)
        callable_or_value = eval(query, globals_env, {})  # noqa: S307
    except NameError:
        callable_or_value = None
    else:
        if callable(callable_or_value):
            callable_value = callable_or_value

            def _callable_predicate(entity: Struct) -> bool:
                return _coerce_mlody_result_to_bool(callable_value(entity), query=query)

            return _callable_predicate

    lambda_env = dict(_MLODY_EVAL_GLOBALS)
    predicate = eval(f"lambda _: ({query})", lambda_env, {})  # noqa: S307

    def _expression_predicate(entity: Struct) -> bool:
        return _coerce_mlody_result_to_bool(predicate(entity), query=query)

    return _expression_predicate


def parse_mlody_segment(entity_query: str | None) -> MlodySegment | None:
    if entity_query is None:
        return None
    expr = parse_traversal_expression(f"[{entity_query}]")
    if not expr.segments:
        return None
    segment = expr.segments[0]
    if isinstance(segment, MlodySegment):
        return segment
    return None


def evaluate_mlody_segment(segment: MlodySegment, entity: object) -> bool:
    predicate = compile_mlody_query_predicate(segment.query)
    struct_entity = struct_like_to_struct(entity)
    if not isinstance(struct_entity, Struct):
        raise TypeError(
            f"@mlody segment expects a Struct-like entity, got {type(entity).__name__}.",
        )
    return predicate(struct_entity)


class _Parser:
    """Single-use parser instance holding cursor state over the input string."""

    def __init__(self, expr: str) -> None:
        self._expr = expr
        self._pos = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse(self) -> PathExpression:
        segments: list[PathSegment] = []
        while self._pos < len(self._expr):
            seg = self._parse_segment()
            segments.append(seg)
        return PathExpression(segments=tuple(segments))

    # ------------------------------------------------------------------
    # Segment dispatch
    # ------------------------------------------------------------------

    def _parse_segment(self) -> PathSegment:
        ch = self._peek()
        if ch == "[":
            return self._parse_bracket_seg()
        if ch == ".":
            return self._parse_dot_seg()
        # Any other character is unexpected (trailing junk, etc.)
        self._fail(f"unexpected character {ch!r}")

    # ------------------------------------------------------------------
    # Bracket segment: "[" ( INT | STR | "*" ) "]"
    # ------------------------------------------------------------------

    def _parse_bracket_seg(self) -> PathSegment:
        start = self._pos
        self._consume("[")  # already peeked "["

        if self._at_end():
            self._fail_at(start, "unterminated '[' — expected integer, quoted key, '*', or slice")

        ch = self._peek()

        # Query segment: [@sql <query>] or [@mlody <expr>]
        if ch == "@":
            return self._parse_query_seg(start)

        # Wildcard: [*]
        if ch == "*":
            self._pos += 1
            self._expect_close_bracket(start)
            return WildcardSegment()

        # Quoted string key: ["..."]
        if ch == '"':
            key = self._parse_quoted_string(start)
            self._expect_close_bracket(start)
            return KeySegment(key=key)

        # Slice with no start: [:stop], [:stop:step], [::step], [:]
        if ch == ":":
            return self._parse_slice_tail(start, start_val=None)

        # Integer (possibly negative) — could be [INT] or [INT:...] (slice)
        if ch == "-" or ch.isdigit():
            index = self._parse_integer(start)
            if not self._at_end() and self._peek() == ":":
                return self._parse_slice_tail(start, start_val=index)
            self._expect_close_bracket(start)
            return IndexSegment(index=index)

        # Anything else is invalid bracket content
        self._fail_at(
            self._pos,
            (
                f"invalid bracket content starting with {ch!r}; "
                "expected an integer, a quoted string (\"...\"), '*', or a slice (e.g. '1:4')"
            ),
        )

    def _parse_query_seg(self, bracket_start: int) -> PathSegment:
        if self._expr[self._pos : self._pos + len("@sql")].lower() == "@sql":
            query, _query_start = self._parse_query_body(
                bracket_start,
                keyword="@sql",
            )
            return SqlSegment(query=query)
        if self._expr[self._pos : self._pos + len("@mlody")].lower() == "@mlody":
            query, query_start = self._parse_query_body(
                bracket_start,
                keyword="@mlody",
            )
            _validate_mlody_query(query, expr=self._expr, query_start=query_start)
            return MlodySegment(query=query)
        self._fail_at(
            self._pos,
            "expected '@sql' or '@mlody' after '['",
        )

    def _parse_query_body(self, bracket_start: int, *, keyword: str) -> tuple[str, int]:
        end = self._pos + len(keyword)
        if self._expr[self._pos : end].lower() != keyword:
            self._fail_at(
                self._pos,
                f"expected '{keyword}' after '['; "
                f"got {self._expr[self._pos:end]!r}",
            )
        self._pos = end
        while not self._at_end() and self._peek() in (" ", "\t"):
            self._pos += 1

        query_start = self._pos
        depth = 1
        in_string: str | None = None
        escaped = False

        while not self._at_end():
            ch = self._peek()
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
                self._pos += 1
                continue

            if ch in ("'", '"'):
                in_string = ch
                self._pos += 1
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            self._pos += 1

        if self._at_end():
            self._fail_at(
                bracket_start,
                f"unterminated '[{keyword}' — missing closing ']'",
            )

        query = self._expr[query_start : self._pos].strip()
        self._pos += 1
        return query, query_start

    def _parse_quoted_string(self, bracket_start: int) -> str:
        """Parse a double-quoted string, consuming both delimiters.

        Returns the content between the quotes (no unescaping — the grammar
        forbids embedded double-quotes: ``STR ::= '"' [^"]* '"'``).
        """
        self._consume('"')
        start = self._pos
        while not self._at_end() and self._peek() != '"':
            self._pos += 1
        if self._at_end():
            self._fail_at(
                bracket_start,
                "unterminated string key in bracket — missing closing '\"'",
            )
        content = self._expr[start : self._pos]
        self._consume('"')
        return content

    def _parse_integer(self, bracket_start: int) -> int:
        """Parse an optional '-' followed by one or more digits."""
        start = self._pos
        if self._peek() == "-":
            self._pos += 1
        if self._at_end() or not self._peek().isdigit():
            self._fail_at(
                bracket_start,
                f"invalid bracket content: expected digit after '-'",
            )
        while not self._at_end() and self._peek().isdigit():
            self._pos += 1
        return int(self._expr[start : self._pos])

    def _parse_slice_tail(self, bracket_start: int, start_val: int | None) -> SliceSegment:
        """Parse the remainder of a slice bracket after the optional start integer.

        Cursor must be positioned at the first ``:``.  Consumes ``:`` + optional
        stop INT + optional ``:`` + optional step INT + ``]``.

        Examples (cursor at first ``:`` in each):
            ``:]``         → SliceSegment(start_val, None, None)
            ``:4]``        → SliceSegment(start_val, 4, None)
            ``:4:2]``      → SliceSegment(start_val, 4, 2)
            ``::2]``       → SliceSegment(start_val, None, 2)
        """
        self._consume(":")  # consume first ':'

        # Parse optional stop
        stop_val: int | None = None
        if not self._at_end() and (self._peek() == "-" or self._peek().isdigit()):
            stop_val = self._parse_integer(bracket_start)

        # Parse optional step
        step_val: int | None = None
        if not self._at_end() and self._peek() == ":":
            self._consume(":")
            if not self._at_end() and (self._peek() == "-" or self._peek().isdigit()):
                step_val = self._parse_integer(bracket_start)

        self._expect_close_bracket(bracket_start)
        return SliceSegment(start=start_val, stop=stop_val, step=step_val)

    def _expect_close_bracket(self, bracket_start: int) -> None:
        if self._at_end() or self._peek() != "]":
            self._fail_at(
                bracket_start,
                "unterminated '[' — missing closing ']'",
            )
        self._pos += 1  # consume "]"

    # ------------------------------------------------------------------
    # Dot-prefix segments: ".." (recursive descent) or "." IDENT (field)
    # ------------------------------------------------------------------

    def _parse_dot_seg(self) -> PathSegment:
        """Parse either a RecursiveDescentSegment ('..') or a FieldSegment ('.IDENT')."""
        pos_of_dot = self._pos
        self._consume(".")

        if self._at_end():
            # A bare trailing "." with nothing after it is invalid
            self._fail_at(pos_of_dot, "bare '.' — expected identifier or '..' for recursive descent")

        if self._peek() == ".":
            # Recursive descent ".."
            self._pos += 1
            return RecursiveDescentSegment()

        # Field segment: IDENT must start with [a-zA-Z_]
        ch = self._peek()
        if not (ch.isalpha() or ch == "_"):
            self._fail_at(
                pos_of_dot,
                (
                    f"'.' must be followed by an identifier "
                    f"([a-zA-Z_][a-zA-Z0-9_]*), got {ch!r}"
                ),
            )

        name = self._parse_ident()
        return FieldSegment(name=name)

    def _parse_ident(self) -> str:
        """Parse IDENT: [a-zA-Z_][a-zA-Z0-9_]*."""
        start = self._pos
        while not self._at_end() and (
            self._peek().isalnum() or self._peek() == "_"
        ):
            self._pos += 1
        return self._expr[start : self._pos]

    # ------------------------------------------------------------------
    # Primitive helpers
    # ------------------------------------------------------------------

    def _peek(self) -> str:
        return self._expr[self._pos]

    def _at_end(self) -> bool:
        return self._pos >= len(self._expr)

    def _consume(self, expected: str) -> None:
        """Advance past ``expected`` character (caller guarantees it's there)."""
        self._pos += len(expected)

    def _fail(self, message: str) -> None:
        raise TraversalParseError(message, self._expr, self._pos)

    def _fail_at(self, position: int, message: str) -> None:
        raise TraversalParseError(message, self._expr, position)


def parse_traversal_expression(expr: str) -> PathExpression:
    """Parse a traversal expression string into a ``PathExpression`` AST.

    Args:
        expr: A UTF-8 string containing a traversal expression (may be empty).

    Returns:
        A ``PathExpression`` on success.

    Raises:
        ``TraversalParseError``: on any syntax error, with the input string,
        position of the first error, and a plain-English description.

    This function is stateless and thread-safe.
    """
    parser = _Parser(expr)
    result = parser.parse()

    # After consuming all recognised segments, the cursor must be at the end.
    # If it is not, there is trailing junk that is not part of any valid segment.
    if parser._pos < len(expr):  # noqa: SLF001
        raise TraversalParseError(
            f"unexpected characters starting at position {parser._pos}: "
            f"{expr[parser._pos:]!r}",
            expr,
            parser._pos,
        )
    return result
