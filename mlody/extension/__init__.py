"""Unified registration surface for mlody extension points.

Import from here to register new struct kinds, pattern matchers, or traversal
engines without coupling your code to internal module paths.

Extension points:

- ``RegisteredStructBase`` — base class for typed struct wrappers; subclass
  and set ``kind = "my_kind"`` to register a new wrapper automatically.
- ``register_pattern`` — decorator factory registering a ``Pattern``
  implementation under a kind name (used by multimethod dispatch).
- ``Pattern`` — Protocol that all pattern classes must satisfy.
- ``register_step_engine`` — register a custom ``StepEngine`` for a new
  traversal segment kind (string key is the segment class name).
- ``StepEngine`` — Protocol that traversal engine classes must satisfy.
"""

from mlody.common._registered_struct import RegisteredStructBase as RegisteredStructBase
from mlody.core.multimethod import Pattern as Pattern
from mlody.core.multimethod import _PATTERN_REGISTRY as _PATTERN_REGISTRY
from mlody.core.multimethod import register_pattern as register_pattern
from mlody.resolver.engine.dispatch import StepEngine as StepEngine
from mlody.resolver.engine.dispatch import register_step_engine as register_step_engine

__all__ = [
    "RegisteredStructBase",
    "register_pattern",
    "Pattern",
    "register_step_engine",
    "StepEngine",
]
