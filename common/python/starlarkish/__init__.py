"""Public API surface for the Starlarkish package."""

from common.python.starlarkish.core.struct import Struct, struct

__all__ = ["Struct", "struct", "Evaluator"]


def __getattr__(name: str) -> object:
    """Lazily expose Evaluator without forcing evaluator deps on core users."""
    if name == "Evaluator":
        from common.python.starlarkish.evaluator.evaluator import Evaluator

        return Evaluator
    raise AttributeError(name)
