"""Completion provider for .mlody files — pure functions over evaluator state."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import tree_sitter
from lsprotocol.types import CompletionItem

from common.python.starlarkish.evaluator.evaluator import SAFE_BUILTINS, Evaluator
from mlody.lsp.parser import find_ancestor, node_at_position

# Keys injected by the evaluator sandbox that are not user symbols.
_FRAMEWORK_INTERNALS: frozenset[str] = frozenset(
    {"__builtins__", "load", "__MLODY__", "builtins"}
)


def _detect_context(
    node: tree_sitter.Node,  # type: ignore[type-arg]
    line_to_cursor: str,
) -> Literal["load_path", "load_symbol", "builtins_member", "general"]:
    """Determine completion context from the AST node at the cursor position.

    Four mutually exclusive contexts, checked in priority order:
    1. load_path    — cursor is inside the path string (first arg) of a load() call
    2. load_symbol  — cursor is inside a symbol string (subsequent arg) of a load() call
    3. builtins_member — cursor follows "builtins."
    4. general      — everything else

    Uses the parse tree for load() detection, enabling correct handling of
    multi-line load() calls where the path string is on a different line from
    the load( opening.
    """
    string_node: tree_sitter.Node | None = (  # type: ignore[type-arg]
        node if node.type == "string" else find_ancestor(node, "string")
    )
    if string_node is not None:
        arg_list = string_node.parent
        if arg_list is not None and arg_list.type == "argument_list":
            call_node = arg_list.parent
            if call_node is not None and call_node.type == "call":
                func = call_node.children[0]
                if func.type == "identifier" and func.text == b"load":
                    string_args = [c for c in arg_list.children if c.type == "string"]
                    if string_args and string_args[0].start_point == string_node.start_point:
                        return "load_path"
                    return "load_symbol"

    if line_to_cursor.rstrip().endswith("builtins."):
        return "builtins_member"

    return "general"


def _load_path_completions(
    partial: str,
    monorepo_root: Path,
    current_file: Path,
) -> list[str]:
    """Return file-path completion candidates for a partial load() path string.

    `partial` is the text inside the string quotes up to the cursor position,
    e.g. "//mlody/" or ":helper".  Resolves `//`-prefixed paths from
    `monorepo_root`, `:`-prefixed paths from `current_file.parent`.
    Returns [] for bare or unrecognised prefixes.
    """
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
    tree: tree_sitter.Tree,
    line: int,
    character: int,
    document_lines: list[str],
) -> list[CompletionItem]:
    """Top-level completion entry point called by the LSP server handler.

    Returns [] if the workspace failed to load (`evaluator` is None).
    Dispatches by cursor context to the appropriate completion source.
    """
    if evaluator is None:
        return []

    line_to_cursor = document_lines[line][:character] if line < len(document_lines) else ""
    node = node_at_position(tree, line, character)
    context = _detect_context(node, line_to_cursor)

    if context == "load_path":
        # Extract the partial path: document text from after the string's opening
        # quote to the cursor.  The string node's start_point marks the opening
        # quote; adding 1 skips it.  In practice load() paths are single-line
        # strings, so start_row == cursor line for all realistic documents.
        string_node: tree_sitter.Node | None = (  # type: ignore[type-arg]
            node if node.type == "string" else find_ancestor(node, "string")
        )
        if string_node is not None:
            start_row, start_col = string_node.start_point
            if start_row == line:
                partial = document_lines[start_row][start_col + 1 : character]
            else:
                # String opened on an earlier line; take from start of cursor line.
                partial = document_lines[line][:character].lstrip("\"' ")
        else:
            partial = ""
        labels = _load_path_completions(partial, monorepo_root, current_file)

    elif context == "load_symbol":
        # Symbol name completions are a future feature (see lsp-completion spec).
        labels = []

    elif context == "builtins_member":
        labels = _builtin_member_completions()

    else:
        labels = _general_completions(evaluator, current_file)

    return [CompletionItem(label=name) for name in labels]
