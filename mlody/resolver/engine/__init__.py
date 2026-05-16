"""Traversal engine package for mlody label resolution.

Importing this package triggers registration of all built-in step engines
via the side-effects of importing each engine module.  After import, the
``step`` function dispatches any segment to its registered engine.

Public surface:
    step                — apply a single traversal step to a MlodyValue
    register_step_engine — register a custom engine for a segment kind
    StepEngine           — Protocol for engine classes
"""

from mlody.resolver.engine import (  # noqa: F401 (import for side-effect registration)
    step_index,
    step_key,
    step_recursive_descent,
    step_slice,
    step_wildcard,
)
from mlody.resolver.engine.dispatch import (
    StepEngine as StepEngine,
    register_step_engine as register_step_engine,
    step as step,
)

__all__ = ["step", "register_step_engine", "StepEngine"]
