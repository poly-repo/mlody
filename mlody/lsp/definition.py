"""Go-to-definition provider for .mlody files — pure functions over evaluator state."""

from __future__ import annotations

import re
from pathlib import Path

from lsprotocol.types import Location, Position, Range

from common.python.starlarkish.evaluator.evaluator import SAFE_BUILTINS, Evaluator

# Matches the path string inside load("...") with the cursor inside it.
# Captures the path including any partial prefix.
_LOAD_PATH_RE = re.compile(r"""load\s*\(\s*["']([^"']*)["']""")

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


def _extract_load_string(line: str, char: int) -> str | None:
    """Return the load() path string if the cursor at `char` is inside one.

    Scans all load("...") occurrences in the line and checks whether `char`
    falls inside the quoted string. Returns the path string or None.
    """
    for m in re.finditer(r"""load\s*\(\s*["']([^"']*)["']""", line):
        # The quoted string spans from the char after the opening quote
        # to the char before the closing quote.
        full_match_start = m.start()
        inner = m.group(1)
        # Find start of the captured group within the full match.
        quote_start = line.index(inner, full_match_start) if inner else (
            m.start(1)
        )
        quote_end = quote_start + len(inner)
        if quote_start <= char <= quote_end:
            return inner
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


def _make_location(path: Path, line: int) -> Location:
    """Construct an LSP Location for a given file path and 0-indexed line."""
    from pygls.uris import from_fs_path  # deferred — pygls is heavy

    pos = Position(line=line, character=0)
    return Location(uri=from_fs_path(str(path)), range=Range(start=pos, end=pos))


def get_definition(
    evaluator: Evaluator | None,
    monorepo_root: Path,
    current_file: Path,
    line: str,
    char: int,
) -> Location | None:
    """Top-level definition entry point called by the LSP server handler.

    Returns None if:
    - the workspace failed to load (evaluator is None)
    - the cursor is not on a navigable token
    - the referenced file or symbol cannot be found

    Two navigation modes (tried in order):
    1. load() path string — navigate to the referenced file at line 0
    2. imported symbol — find definition line in the source file

    Transitive imports (symbols not in the current file's direct load() calls)
    return None intentionally (see design.md §Decisions #7).
    """
    if evaluator is None:
        return None

    # Mode 1: cursor inside a load("...") string → navigate to the file.
    load_str = _extract_load_string(line, char)
    if load_str is not None:
        target_path = _resolve_load_path(load_str, monorepo_root, current_file)
        if target_path is None:
            return None
        return _make_location(target_path, 0)

    # Mode 2: cursor on a symbol name → find its definition via direct imports.
    symbol = _extract_symbol_at_cursor(line, char)
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

    # Scan load() statements in the current file to find the source file.
    try:
        source_lines = current_file.read_text().splitlines()
    except OSError:
        return None

    for source_line in source_lines:
        for load_m in re.finditer(r"""load\s*\(\s*["']([^"']+)["'](.*)""", source_line):
            imports_section = load_m.group(2)
            if re.search(r'["\']\s*' + re.escape(symbol) + r'\s*["\']', imports_section):
                load_path_str = load_m.group(1)
                source_file = _resolve_load_path(
                    load_path_str, monorepo_root, current_file
                )
                if source_file is None:
                    continue
                def_line = _find_symbol_line(source_file, symbol)
                if def_line is not None:
                    return _make_location(source_file, def_line)

    return None
