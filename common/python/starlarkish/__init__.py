"""Public API surface for the Starlarkish package.

Exports the three primary symbols consumers need:

* ``Struct`` — the immutable, hashable, equality-comparable value type
* ``struct`` — the factory function (coerces nested dicts to Struct)
* ``Evaluator`` — the sandboxed .mlody script execution engine

Existing import paths (e.g.
``from common.python.starlarkish.core.struct import struct``) continue to
work unchanged.
"""
from common.python.starlarkish.core.struct import Struct, struct
from common.python.starlarkish.evaluator.evaluator import Evaluator

__all__ = ["Struct", "struct", "Evaluator"]
