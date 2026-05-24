"""Exit codes and typed error hierarchy for mlody-image-builder."""

from __future__ import annotations

import enum

from common.python.errors import StructuredError


class ExitCode(enum.IntEnum):
    SUCCESS = 0
    CLONE_FAILURE = 2
    BUILD_FAILURE = 3
    PUSH_FAILURE = 4


class BuilderError(StructuredError):
    """Base class for all pipeline errors.

    Carries the exit code and a human-readable message. Subclasses add
    structured context (e.g. affected targets, stderr from subprocess).
    """

    def __init__(
        self,
        message: str,
        exit_code: ExitCode,
        **context: object,
    ) -> None:
        # int() cast is required: ExitCode is an IntEnum but StructuredError.exit_code is int
        super().__init__(message, exit_code=int(exit_code), **context)


class CloneError(BuilderError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message, ExitCode.CLONE_FAILURE, **context)


class BazelBuildError(BuilderError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message, ExitCode.BUILD_FAILURE, **context)


class PushError(BuilderError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message, ExitCode.PUSH_FAILURE, **context)


class ResolveRefError(BuilderError):
    """Raised when a git ref cannot be resolved to a full SHA."""

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message, ExitCode.CLONE_FAILURE, **context)
