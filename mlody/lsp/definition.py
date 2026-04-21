"""Go-to-definition provider for .mlody files — pure functions over evaluator state."""

from __future__ import annotations

import re
from pathlib import Path

import tree_sitter
from lsprotocol.types import Location, Position, Range

from common.python.starlarkish.evaluator.evaluator import SAFE_BUILTINS, Evaluator
from mlody.lsp.parser import find_ancestor, get_load_statements, node_at_position

# Matches a symbol definition line: `NAME = ...` or `def NAME`.
_ASSIGNMENT_RE = re.compile(r"^(\w+)\s*=")
_DEF_RE = re.compile(r"^def\s+(\w+)")

# Identifier characters for word-boundary scan.
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _resolve_load_path(
    load_str: str,
    monorepo_root: Path,
    current_file: Path,
) -> Path | None:
    """Resolve a load() path string to an absolute filesystem path.

    Returns None if the file does not exist on disk.
    """
    if load_str.startswith("//"):
        target = monorepo_root / load_str[2:]
    elif load_str.startswith(":"):
        target = current_file.parent / load_str[1:]
    else:
        # Relative bare path — resolve from current file's directory.
        target = current_file.parent / load_str

    resolved = target.resolve()
    return resolved if resolved.exists() else None


def _find_symbol_line(source_path: Path, symbol: str) -> int | None:
    """Return the 0-indexed line number where `symbol` is defined in `source_path`.

    Recognises two patterns:
      - Assignment:  SYMBOL = ...
      - Function:    def SYMBOL(...)

    Returns None if the symbol is not found or the file cannot be read.
    """
    try:
        lines = source_path.read_text().splitlines()
    except OSError:
        return None

    for i, line in enumerate(lines):
        m_assign = _ASSIGNMENT_RE.match(line)
        if m_assign and m_assign.group(1) == symbol:
            return i
        m_def = _DEF_RE.match(line)
        if m_def and m_def.group(1) == symbol:
            return i

    return None


def _extract_symbol_at_cursor(line: str, char: int) -> str | None:
    """Return the identifier token under the cursor position.

    Scans identifier tokens in the line and returns whichever one contains
    `char`. Returns None if the cursor is not on an identifier.
    """
    for m in _IDENT_RE.finditer(line):
        if m.start() <= char <= m.end():
            return m.group(0)
    return None


def _load_string_at_cursor(
    node: tree_sitter.Node,  # type: ignore[type-arg]
    tree: tree_sitter.Tree,
) -> tuple[str, bool] | None:
    """Return (string_value, is_path) if the cursor is on a load() string arg, else None.

    is_path=True  → first argument of load() (the path string)
    is_path=False → subsequent argument of load() (an imported symbol name)

    Uses start_point equality for first-arg detection instead of `is` identity
    because tree-sitter Python bindings return new wrapper objects on each
    .children access (D4 in design.md).
    """
    string_node: tree_sitter.Node | None = (  # type: ignore[type-arg]
        node if node.type == "string" else find_ancestor(node, "string")
    )
    if string_node is None:
        return None

    arg_list = string_node.parent
    if arg_list is None or arg_list.type != "argument_list":
        return None

    call_node = arg_list.parent
    if call_node is None or call_node.type != "call":
        return None

    func = call_node.children[0]
    if func.type != "identifier" or func.text != b"load":
        return None

    string_args = [c for c in arg_list.children if c.type == "string"]
    if not string_args:
        return None

    raw = string_node.text or b'""'
    value = raw.decode().strip('"').strip("'")

    # First string arg is the path; all others are imported symbol names.
    is_path = string_args[0].start_point == string_node.start_point
    return (value, is_path)


def _make_location(path: Path, line: int) -> Location:
    """Construct an LSP Location for a given file path and 0-indexed line."""
    from pygls.uris import from_fs_path  # deferred — pygls is heavy

    pos = Position(line=line, character=0)
    return Location(uri=from_fs_path(str(path)), range=Range(start=pos, end=pos))


def get_definition(
    evaluator: Evaluator | None,
    monorepo_root: Path,
    current_file: Path,
    tree: tree_sitter.Tree,
    line: int,
    char: int,
    document_lines: list[str],
) -> Location | None:
    """Top-level definition entry point called by the LSP server handler.

    Returns None if:
    - the workspace failed to load (evaluator is None)
    - the cursor is not on a navigable token
    - the referenced file or symbol cannot be found

    Three navigation modes (tried in order):
    1. load() path string — navigate to the referenced file at line 0
    2. load() symbol string — navigate to the symbol's definition in the loaded file
    3. imported symbol identifier — find definition line via direct load() imports

    Transitive imports (symbols not in the current file's direct load() calls)
    return None intentionally (see design.md §Decisions #7).
    """
    if evaluator is None:
        return None

    # Modes 1 and 2: cursor inside a load() string argument (path or symbol).
    # Uses parse-tree traversal to handle multi-line load() calls correctly.
    node = node_at_position(tree, line, char)
    load_result = _load_string_at_cursor(node, tree)
    if load_result is not None:
        string_value, is_path = load_result
        if is_path:
            # Mode 1: navigate to the referenced file.
            target_path = _resolve_load_path(string_value, monorepo_root, current_file)
            return _make_location(target_path, 0) if target_path else None
        else:
            # Mode 2: cursor on a symbol string — navigate to its definition.
            # Find the load() statement that declares this symbol and resolve.
            for stmt in get_load_statements(tree):
                for sym in stmt.symbols:
                    if sym.name == string_value:
                        source_file = _resolve_load_path(
                            stmt.path, monorepo_root, current_file
                        )
                        if source_file is None:
                            return None
                        def_line = _find_symbol_line(source_file, string_value)
                        return _make_location(source_file, def_line) if def_line is not None else None
            return None

    # Mode 3: cursor on a symbol name identifier.
    line_text = document_lines[line] if line < len(document_lines) else ""
    symbol = _extract_symbol_at_cursor(line_text, char)
    if symbol is None:
        return None

    # Builtins are not navigable.
    if symbol in SAFE_BUILTINS:
        return None

    # Find which directly-loaded file provides this symbol.
    # Only inspect files that the current file explicitly loads — not transitive.
    # pyright: ignore — _module_globals is a private attribute
    current_globals: dict[str, object] = evaluator._module_globals.get(  # type: ignore[attr-defined]
        current_file, {}
    )
    if symbol not in current_globals:
        return None

    # Use parse-tree traversal instead of a regex line-scan so that multi-line
    # load() forms (symbol on a different line from load() opening) are handled.
    for stmt in get_load_statements(tree):
        for sym in stmt.symbols:
            if sym.name == symbol:
                source_file = _resolve_load_path(
                    stmt.path, monorepo_root, current_file
                )
                if source_file is None:
                    continue
                def_line = _find_symbol_line(source_file, symbol)
                if def_line is not None:
                    return _make_location(source_file, def_line)

    return None
