"""Lazy reference to a workspace context field for use as a value() default."""
from __future__ import annotations

from dataclasses import dataclass

from common.python.starlarkish.core.struct import struct


@dataclass(frozen=True)
class ContextRef:
    """Deferred reference to a workspace context field.

    Stored verbatim in RegisteredValue.default and resolved at
    value-normalization time from the live workspace context — so the correct
    value is picked up per workspace fork rather than at eval time.

    ``ctx_path`` is a dotted attribute chain on the evaluator's ``_extra_ctx``
    (e.g. ``"workspace.commit"`` resolves as
    ``_extra_ctx.workspace.commit``).
    """

    source: str    # public label, e.g. "cfg.sha" — used in lineage source strings
    ctx_path: str  # dotted path into _extra_ctx, e.g. "workspace.commit"

    # Class-level sentinel for duck-typing inside the Starlark sandbox where
    # isinstance() is unavailable; detected via python.hasattr().
    _is_context_ref = True


def build_cfg_struct() -> object:
    """Return the ``cfg`` Starlark global — a Struct of ContextRef sentinels."""
    return struct(
        sha=ContextRef("cfg.sha", "workspace.commit"),
        user=ContextRef("cfg.user", "workspace.user"),
        branch=ContextRef("cfg.branch", "workspace.branch"),
        directory=ContextRef("cfg.directory", "workspace.directory"),
        run_id=ContextRef("cfg.run_id", "run.id"),
    )
