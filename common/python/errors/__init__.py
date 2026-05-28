"""Common base for all structured pipeline errors.

This package has no dependencies on any mlody package or common.python.console.
It is safe to import from anywhere in the repository.
"""

from __future__ import annotations


class StructuredError(Exception):
    """Base for structured errors carrying exit_code, message, and optional context."""

    exit_code: int
    message: str
    context: dict[str, object]

    def __init__(self, message: str, *, exit_code: int = 1, **context: object) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.context = dict(context)
