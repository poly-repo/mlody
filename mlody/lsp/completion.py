"""Completion provider for .mlody files — pure functions over evaluator state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from lsprotocol.types import CompletionItem

from common.python.starlarkish.evaluator.evaluator import SAFE_BUILTINS, Evaluator

# Keys injected by the evaluator sandbox that are not user symbols.
_FRAMEWORK_INTERNALS: frozenset[str] = frozenset(
    {"__builtins__", "load", "__MLODY__", "builtins"}
)

# Regex: matches the partial path string inside a load("...") call.
# Captures everything after the opening quote up to end-of-string.
_LOAD_RE = re.compile(r"""load\s*\(\s*["']([^"']*)$""")


def _detect_context(line: str) -> Literal["load_path", "builtins_member", "general"]:
    """Determine completion context from the line text up to the cursor.

    Three mutually exclusive contexts, checked in priority order:
    1. load_path  — cursor is inside a load("...") string literal
    2. builtins_member — cursor follows "builtins."
    3. general    — everything else
    """
    if _LOAD_RE.search(line):
        return "load_path"
    if line.rstrip().endswith("builtins."):
        return "builtins_member"
    return "general"


def _load_path_completions(
    line: str,
    monorepo_root: Path,
    current_file: Path,
) -> list[str]:
    """Return file-path completion candidates for a load("...") string.

    Resolves `//`-prefixed paths from `monorepo_root`, `:`-prefixed paths
    from `current_file.parent`. Returns [] for bare or unrecognised prefixes.
    """
    m = _LOAD_RE.search(line)
    if not m:
        return []

    partial = m.group(1)  # everything typed so far inside the quotes

    if partial.startswith("//"):
        relative = partial[2:]  # strip //
        base = monorepo_root
    elif partial.startswith(":"):
        relative = partial[1:]  # strip :
        base = current_file.parent
    else:
        # Bare prefix with no recognised scheme — offer nothing to avoid noise.
        return []

    # Split into directory portion and the partial filename being typed.
    if "/" in relative:
        dir_part, _ = relative.rsplit("/", 1)
        search_dir = base / dir_part
    else:
        search_dir = base

    if not search_dir.is_dir():
        return []

    results: list[str] = []
    for entry in sorted(search_dir.iterdir()):
        if entry.is_dir():
            results.append(entry.name + "/")
        elif entry.suffix == ".mlody":
            results.append(entry.name)
    return results


def _builtin_member_completions() -> list[str]:
    """Return the member names available on the `builtins` object."""
    # Matches the Builtins class attributes in starlarkish/evaluator/evaluator.py.
    return ["register", "ctx"]


def _general_completions(evaluator: Evaluator, current_file: Path) -> list[str]:
    """Return safe builtins plus symbols loaded into the current file.

    Accesses evaluator._module_globals directly — intentional coupling to the
    starlarkish implementation; documented in design.md §Decisions #4.
    """
    names: list[str] = list(SAFE_BUILTINS.keys())

    # pyright: ignore — _module_globals is a private attribute
    module_globals: dict[str, object] = evaluator._module_globals.get(  # type: ignore[attr-defined]
        current_file, {}
    )
    for key in module_globals:
        if key not in _FRAMEWORK_INTERNALS and not key.startswith("_"):
            names.append(key)

    return names


def get_completions(
    evaluator: Evaluator | None,
    monorepo_root: Path,
    current_file: Path,
    line: str,
) -> list[CompletionItem]:
    """Top-level completion entry point called by the LSP server handler.

    Returns [] if the workspace failed to load (`evaluator` is None).
    Dispatches by cursor context to the appropriate completion source.
    """
    if evaluator is None:
        return []

    context = _detect_context(line)

    if context == "load_path":
        labels = _load_path_completions(line, monorepo_root, current_file)
    elif context == "builtins_member":
        labels = _builtin_member_completions()
    else:
        labels = _general_completions(evaluator, current_file)

    return [CompletionItem(label=name) for name in labels]
