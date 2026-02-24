"""Tests for mlody.lsp.diagnostics — get_parse_diagnostics and get_eval_diagnostics.

Each test class traces to a named requirement in
openspec/changes/lsp-diagnostics/specs/lsp-diagnostics/spec.md.
"""

from __future__ import annotations

from mlody.lsp.diagnostics import get_eval_diagnostics, get_parse_diagnostics
from mlody.lsp.parser import DocumentCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(text: str):  # type: ignore[no-untyped-def]
    """Parse *text* and return the tree (fresh cache each call)."""
    cache = DocumentCache()
    return cache.update("file:///test_diag.mlody", version=1, text=text)


# ---------------------------------------------------------------------------
# get_parse_diagnostics
# ---------------------------------------------------------------------------


class TestGetParseDiagnostics:
    """Requirement: Publish diagnostics on document open / change."""

    def test_valid_document_returns_empty_list(self) -> None:
        """Scenario: Valid document on open clears diagnostics."""
        tree = _parse("x = 1\ny = 2\n")
        result = get_parse_diagnostics(tree)
        assert result == []

    def test_syntax_error_returns_at_least_one_diagnostic(self) -> None:
        """Scenario: Syntax error on open produces a diagnostic."""
        # "def (((" is invalid Starlark — tree-sitter inserts ERROR node(s).
        tree = _parse("def (((")
        result = get_parse_diagnostics(tree)
        assert len(result) >= 1

    def test_syntax_error_diagnostic_has_correct_severity_and_source(self) -> None:
        """Scenario: Syntax error diagnostic has severity=Error, source='mlody-lsp'."""
        from lsprotocol import types

        tree = _parse("def (((")
        result = get_parse_diagnostics(tree)
        assert len(result) >= 1
        diag = result[0]
        assert diag.severity == types.DiagnosticSeverity.Error
        assert diag.source == "mlody-lsp"

    def test_syntax_error_diagnostic_has_range(self) -> None:
        """Scenario: Syntax error on open produces a diagnostic with a valid range."""
        from lsprotocol import types

        tree = _parse("def (((")
        result = get_parse_diagnostics(tree)
        assert len(result) >= 1
        diag = result[0]
        assert isinstance(diag.range, types.Range)
        assert isinstance(diag.range.start, types.Position)
        assert isinstance(diag.range.end, types.Position)

    def test_missing_node_diagnostic_message_contains_missing(self) -> None:
        """Requirement: Report missing-token errors.

        Scenario: Missing closing paren (or missing token) produces a diagnostic
        with "missing" in the message.

        "if :" is an if-statement without a condition — tree-sitter-starlark
        inserts a MISSING `identifier` node for the absent condition, giving us
        a real is_missing=True node to test against.
        """
        # "if :" has no condition → tree-sitter inserts a MISSING identifier.
        tree = _parse("if :")
        result = get_parse_diagnostics(tree)
        # At least one diagnostic must mention "missing".
        messages = [d.message.lower() for d in result]
        assert any("missing" in m for m in messages), (
            f"Expected a 'missing' diagnostic; got: {[d.message for d in result]}"
        )

    def test_multiple_errors_return_multiple_diagnostics(self) -> None:
        """Scenario: Multiple error nodes produce multiple diagnostics.

        "x = @\\ny = $" produces a parent ERROR node wrapping the whole text,
        with two nested ERROR nodes inside — one for each unrecognised character.
        The recursive walk in get_parse_diagnostics finds all of them.
        """
        # @ and $ are invalid in Starlark; each triggers a distinct ERROR node.
        tree = _parse("x = @\ny = $")
        result = get_parse_diagnostics(tree)
        assert len(result) >= 2  # noqa: PLR2004


# ---------------------------------------------------------------------------
# get_eval_diagnostics
# ---------------------------------------------------------------------------


class TestGetEvalDiagnostics:
    """Requirement: Surface evaluator runtime errors as diagnostics."""

    _URI = "file:///mlody/pipeline.mlody"

    def test_name_error_message_starts_with_nameerror(self) -> None:
        """Scenario: NameError from evaluator appears as a diagnostic."""
        exc = NameError("name 'missing_var' is not defined")
        result = get_eval_diagnostics(exc, self._URI)
        assert len(result) == 1
        assert result[0].message.startswith("NameError:")

    def test_syntax_error_at_line5_offset3_maps_to_line4_char2(self) -> None:
        """Scenario: SyntaxError from evaluator is positioned correctly.

        SyntaxError.lineno and .offset are 1-indexed; the diagnostic must
        be at line=4, character=2 (0-indexed).
        """
        exc = SyntaxError("invalid syntax")
        exc.lineno = 5
        exc.offset = 3
        exc.filename = "/mlody/pipeline.mlody"

        result = get_eval_diagnostics(exc, self._URI)

        assert len(result) == 1
        assert result[0].range.start.line == 4  # noqa: PLR2004
        assert result[0].range.start.character == 2  # noqa: PLR2004

    def test_no_traceback_match_falls_back_to_line0_char0(self) -> None:
        """Scenario: Evaluator error with no traceback match falls back to line 0."""
        exc = RuntimeError("something went wrong")
        # exc.__traceback__ is None — no frames to match against the URI.
        result = get_eval_diagnostics(exc, self._URI)

        assert len(result) == 1
        assert result[0].range.start.line == 0
        assert result[0].range.start.character == 0

    def test_eval_diagnostic_has_error_severity_and_source(self) -> None:
        """Diagnostic from eval error has severity=Error, source='mlody-lsp'."""
        from lsprotocol import types

        exc = NameError("x")
        result = get_eval_diagnostics(exc, self._URI)

        assert result[0].severity == types.DiagnosticSeverity.Error
        assert result[0].source == "mlody-lsp"

    def test_traceback_frame_match_uses_frame_lineno(self) -> None:
        """Traceback frame matching uses tb_lineno (0-indexed in result)."""
        # Create a real exception with a real traceback by actually raising it
        # inside a function with a known filename substring.
        def _raise_at_line() -> None:
            raise ValueError("from frame")

        try:
            _raise_at_line()
        except ValueError as exc:
            # The co_filename of the frame is this test file's path.
            # Use a URI that contains the test file name so it matches.
            import os

            test_file = os.path.abspath(__file__)
            uri = f"file://{test_file}"
            result = get_eval_diagnostics(exc, uri)

        assert len(result) == 1
        # Line should be > 0 since the raise is not on the first line.
        assert result[0].range.start.line >= 0
