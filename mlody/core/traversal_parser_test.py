"""Tests for mlody.core.traversal_parser — parse_traversal_expression.

Each test traces back to a named scenario in the spec:
  openspec/changes/mlody-label-traversal/specs/traversal-grammar/spec.md
"""

from __future__ import annotations

import pytest

from common.python.starlarkish.core.struct import Struct
from mlody.core.traversal_grammar import (
    FieldSegment,
    IndexSegment,
    KeySegment,
    MlodySegment,
    PathExpression,
    RecursiveDescentSegment,
    TraversalParseError,
    WildcardSegment,
)
from mlody.core.traversal_parser import evaluate_mlody_segment, parse_traversal_expression


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


class TestParseSuccess:
    """Requirement: parse_traversal_expression function — success scenarios."""

    def test_empty_string_returns_empty_expression(self) -> None:
        """Scenario: Empty string returns empty PathExpression."""
        result = parse_traversal_expression("")
        assert result == PathExpression(segments=())
        assert len(result) == 0

    def test_single_field_segment(self) -> None:
        """Scenario: Single FieldSegment parsed correctly."""
        result = parse_traversal_expression(".loss")
        assert result == PathExpression(segments=(FieldSegment(name="loss"),))

    def test_index_segment_positive(self) -> None:
        """Scenario: IndexSegment with positive integer parsed correctly."""
        result = parse_traversal_expression("[3]")
        assert result == PathExpression(segments=(IndexSegment(index=3),))

    def test_index_segment_negative(self) -> None:
        """Scenario: IndexSegment with negative integer parsed correctly."""
        result = parse_traversal_expression("[-1]")
        assert result == PathExpression(segments=(IndexSegment(index=-1),))

    def test_index_segment_zero(self) -> None:
        result = parse_traversal_expression("[0]")
        assert result == PathExpression(segments=(IndexSegment(index=0),))

    def test_key_segment(self) -> None:
        """Scenario: KeySegment parsed correctly."""
        result = parse_traversal_expression('["f1"]')
        assert result == PathExpression(segments=(KeySegment(key="f1"),))

    def test_wildcard_segment_bracket_form(self) -> None:
        """Scenario: WildcardSegment from bracket form parsed correctly."""
        result = parse_traversal_expression("[*]")
        assert result == PathExpression(segments=(WildcardSegment(),))

    def test_recursive_descent_segment(self) -> None:
        """Scenario: RecursiveDescentSegment parsed correctly."""
        result = parse_traversal_expression("..")
        assert result == PathExpression(segments=(RecursiveDescentSegment(),))

    def test_multi_segment_expression(self) -> None:
        """Scenario: Multi-segment expression parsed in order."""
        result = parse_traversal_expression('.config[0]["lr"]')
        assert result == PathExpression(
            segments=(FieldSegment("config"), IndexSegment(0), KeySegment("lr"))
        )

    def test_field_then_index(self) -> None:
        result = parse_traversal_expression(".items[2]")
        assert result == PathExpression(
            segments=(FieldSegment("items"), IndexSegment(2))
        )

    def test_wildcard_then_field(self) -> None:
        result = parse_traversal_expression("[*].loss")
        assert result == PathExpression(
            segments=(WildcardSegment(), FieldSegment("loss"))
        )

    def test_recursive_descent_then_field(self) -> None:
        result = parse_traversal_expression("...loss")
        # ".." is the recursive descent, ".loss" is the field
        assert result == PathExpression(
            segments=(RecursiveDescentSegment(), FieldSegment("loss"))
        )

    def test_field_with_underscore(self) -> None:
        result = parse_traversal_expression(".field_name")
        assert result == PathExpression(segments=(FieldSegment("field_name"),))

    def test_field_with_digits(self) -> None:
        result = parse_traversal_expression(".field2")
        assert result == PathExpression(segments=(FieldSegment("field2"),))

    def test_key_with_special_chars(self) -> None:
        """Key can contain any characters that are not double-quote."""
        result = parse_traversal_expression('["f1_score"]')
        assert result == PathExpression(segments=(KeySegment(key="f1_score"),))

    def test_mlody_segment(self) -> None:
        result = parse_traversal_expression("[@mlody _.kind == 'task']")
        assert result == PathExpression(
            segments=(MlodySegment(query="_.kind == 'task'"),),
        )

    def test_mlody_segment_preserves_nested_brackets(self) -> None:
        result = parse_traversal_expression(
            "[@mlody _.kind in ['task', 'action']]",
        )
        assert result == PathExpression(
            segments=(MlodySegment(query="_.kind in ['task', 'action']"),),
        )

    def test_mlody_segment_evaluates_against_struct(self) -> None:
        segment = MlodySegment(query="_.kind == 'task'")
        assert evaluate_mlody_segment(segment, Struct(kind="task", name="train")) is True
        assert evaluate_mlody_segment(segment, Struct(kind="value", name="train")) is False


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestParseErrors:
    """Requirement: parse_traversal_expression function — error scenarios."""

    def test_invalid_bracket_content_raises(self) -> None:
        """Scenario: Invalid bracket content raises TraversalParseError."""
        with pytest.raises(TraversalParseError) as exc_info:
            parse_traversal_expression("[foo]")
        exc = exc_info.value
        assert exc.input_expr == "[foo]"
        assert exc.position >= 0

    def test_unterminated_bracket_raises(self) -> None:
        """Scenario: Unterminated bracket raises TraversalParseError."""
        with pytest.raises(TraversalParseError) as exc_info:
            parse_traversal_expression("[2")
        exc = exc_info.value
        assert exc.input_expr == "[2"

    def test_bare_dot_raises(self) -> None:
        """Scenario: Bare dot with no identifier raises TraversalParseError.

        A single "." is neither a field segment (needs IDENT) nor ".."
        (needs second dot).
        """
        with pytest.raises(TraversalParseError) as exc_info:
            parse_traversal_expression(".")
        exc = exc_info.value
        assert exc.input_expr == "."

    def test_trailing_junk_raises(self) -> None:
        """Scenario: Trailing junk after valid expression raises TraversalParseError."""
        with pytest.raises(TraversalParseError) as exc_info:
            parse_traversal_expression(".loss!!")
        exc = exc_info.value
        assert exc.input_expr == ".loss!!"

    def test_error_carries_position(self) -> None:
        """TraversalParseError.position indicates where the error was detected."""
        with pytest.raises(TraversalParseError) as exc_info:
            parse_traversal_expression("[foo]")
        # Position should point somewhere inside/at the invalid content
        assert exc_info.value.position >= 0

    def test_unterminated_string_in_bracket_raises(self) -> None:
        """Unterminated string key raises TraversalParseError."""
        with pytest.raises(TraversalParseError):
            parse_traversal_expression('["unclosed')

    def test_empty_field_name_raises(self) -> None:
        """Dot followed immediately by non-IDENT character raises."""
        with pytest.raises(TraversalParseError):
            parse_traversal_expression(".123")  # digit-only after dot without ..

    def test_invalid_mlody_expression_raises(self) -> None:
        with pytest.raises(TraversalParseError, match="invalid @mlody expression"):
            parse_traversal_expression("[@mlody _.kind == ]")


# ---------------------------------------------------------------------------
# Round-trip fidelity
# Scenario: "Round-trip for single FieldSegment"
# Scenario: "Round-trip for mixed multi-segment expression"
# Scenario: "Round-trip for RecursiveDescentSegment"
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Requirement: Round-trip fidelity — str(parse(str(expr))) == str(expr)."""

    def _roundtrip(self, expr: PathExpression) -> PathExpression:
        return parse_traversal_expression(str(expr))

    def test_round_trip_single_field(self) -> None:
        """Scenario: Round-trip for single FieldSegment."""
        expr = PathExpression(segments=(FieldSegment("x"),))
        assert self._roundtrip(expr) == expr

    def test_round_trip_mixed_multi_segment(self) -> None:
        """Scenario: Round-trip for mixed multi-segment expression."""
        expr = PathExpression(
            segments=(FieldSegment("a"), IndexSegment(1), WildcardSegment())
        )
        assert self._roundtrip(expr) == expr

    def test_round_trip_recursive_descent(self) -> None:
        """Scenario: Round-trip for RecursiveDescentSegment."""
        expr = PathExpression(
            segments=(RecursiveDescentSegment(), FieldSegment("loss"))
        )
        assert self._roundtrip(expr) == expr

    def test_round_trip_key_segment(self) -> None:
        expr = PathExpression(segments=(KeySegment("f1_score"),))
        assert self._roundtrip(expr) == expr

    def test_round_trip_negative_index(self) -> None:
        expr = PathExpression(segments=(IndexSegment(-1),))
        assert self._roundtrip(expr) == expr

    def test_round_trip_complex_path(self) -> None:
        expr = PathExpression(
            segments=(
                FieldSegment("config"),
                IndexSegment(0),
                KeySegment("lr"),
                WildcardSegment(),
                RecursiveDescentSegment(),
                FieldSegment("loss"),
            )
        )
        assert self._roundtrip(expr) == expr

    def test_round_trip_empty(self) -> None:
        expr = PathExpression(segments=())
        assert self._roundtrip(expr) == expr

    def test_round_trip_mlody_segment(self) -> None:
        expr = PathExpression(segments=(MlodySegment("_.kind == 'task'"),))
        assert self._roundtrip(expr) == expr
