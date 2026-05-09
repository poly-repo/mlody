"""Treesitter-based extractor of entity registration call line ranges.

Parses a .mlody source file and returns a mapping from (kind, name) to
(start_line, end_line) — 1-based inclusive — for every statically-identifiable
registration call found anywhere in the file.

The extractor performs a full AST walk (not just top-level statements), so
nested registration calls — e.g. ``action(...)`` passed directly as a keyword
argument to ``task(...)`` — are captured at any depth.

Two call forms are recognised at any nesting level:

  Direct:  builtins.register("kind", struct(name="foo", ...))
  Helper:  root("foo", ...)  /  task(name="foo", ...)  /  action("foo", ...)
           and any other name in _HELPER_KINDS

Special helper expansion:

  cached_value(name="foo", ...) contributes source ranges for both
  ("value", "foo") and ("value", "foo-remote") unless a literal
  remote_name="..." overrides the synthesized name.

The entity name is extracted from either the first positional string argument
or a ``name=`` keyword argument. Entities whose name is not a literal string
are silently skipped. Duplicate (kind, name) pairs within the same file raise
``ValueError``. ``ERROR`` nodes are skipped along with their subtrees; nodes
that merely contain errors are still descended into so valid siblings survive.

Node types verified against tree-sitter-starlark 1.3.0.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_starlark as _ts_starlark
except ImportError as _exc:
    raise ImportError(
        "tree-sitter and tree-sitter-starlark are required for source_parser. "
        "Run: o-repin"
    ) from _exc

STARLARK_LANGUAGE: tree_sitter.Language = tree_sitter.Language(_ts_starlark.language())
_parser: tree_sitter.Parser = tree_sitter.Parser(STARLARK_LANGUAGE)

# Map from helper function name -> registration kind.
_HELPER_KINDS: dict[str, str] = {
    "root": "root",
    "task": "task",
    "action": "action",
    "value": "value",
    "location": "location",
    "type": "type",
}


def _string_value(node: tree_sitter.Node) -> str | None:  # type: ignore[type-arg]
    """Return the unquoted string value for a tree-sitter string node, or None."""
    if node.type != "string":
        return None
    raw = node.text
    if raw is None:
        return None
    text = raw.decode()
    # Strip surrounding quotes (single or double, single char each side).
    if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
        return text[1:-1]
    return None


def _extract_name_from_struct_call(
    arg_node: tree_sitter.Node,  # type: ignore[type-arg]
) -> str | None:
    """Return the value of the ``name=`` keyword arg from a struct(...) call node."""
    if arg_node.type != "call":
        return None
    if arg_node.child_count < 2:
        return None
    func = arg_node.children[0]
    if func.type != "identifier" or func.text != b"struct":
        return None
    arg_list = arg_node.children[1]
    if arg_list.type != "argument_list":
        return None
    for child in arg_list.children:
        if child.type == "keyword_argument":
            kw_children = child.children
            # keyword_argument: identifier "=" value
            if len(kw_children) >= 3:
                key_node = kw_children[0]
                val_node = kw_children[2]
                if key_node.type == "identifier" and key_node.text == b"name":
                    return _string_value(val_node)
    return None


def _first_positional_string(
    arg_list: tree_sitter.Node,  # type: ignore[type-arg]
) -> str | None:
    """Return the value of the first positional string argument in an argument_list node."""
    for child in arg_list.children:
        if child.type == "string":
            return _string_value(child)
    return None


def _keyword_arg_name(
    arg_list: tree_sitter.Node,  # type: ignore[type-arg]
) -> str | None:
    """Return the string value of the ``name=`` keyword argument, or None."""
    return _keyword_arg_string(arg_list, "name")


def _keyword_arg_string(
    arg_list: tree_sitter.Node,  # type: ignore[type-arg]
    key_name: str,
) -> str | None:
    """Return the string value of a keyword argument, or None."""
    for child in arg_list.children:
        if child.type == "keyword_argument" and len(child.children) >= 3:
            key_node = child.children[0]
            val_node = child.children[2]
            if key_node.type == "identifier" and key_node.text == key_name.encode():
                return _string_value(val_node)
    return None


def _has_keyword_arg(
    arg_list: tree_sitter.Node,  # type: ignore[type-arg]
    key_name: str,
) -> bool:
    for child in arg_list.children:
        if child.type == "keyword_argument" and len(child.children) >= 1:
            key_node = child.children[0]
            if key_node.type == "identifier" and key_node.text == key_name.encode():
                return True
    return False


def _cached_value_entries(
    arg_list: tree_sitter.Node,  # type: ignore[type-arg]
) -> list[tuple[str, str]]:
    """Expand cached_value(...) into the value names it will register."""
    name = _first_positional_string(arg_list)
    if name is None:
        name = _keyword_arg_name(arg_list)
    if name is None:
        return []

    remote_name = _keyword_arg_string(arg_list, "remote_name")
    if remote_name is None:
        if _has_keyword_arg(arg_list, "remote_name"):
            return [("value", name)]
        remote_name = name + "-remote"

    return [("value", name), ("value", remote_name)]


def _process_call(
    call_node: tree_sitter.Node,  # type: ignore[type-arg]
) -> list[tuple[str, str]]:
    """Return registration entries from a call node, or an empty list."""
    if call_node.child_count < 2:
        return []

    func_node = call_node.children[0]
    arg_list = call_node.children[1]
    if arg_list.type != "argument_list":
        return []

    # Direct form: builtins.register("kind", struct(name="foo", ...))
    if func_node.type == "attribute":
        attr_children = func_node.children
        # attribute: object "." attribute_name
        if len(attr_children) >= 3:
            obj = attr_children[0]
            attr = attr_children[2]
            if (
                obj.type == "identifier"
                and obj.text == b"builtins"
                and attr.type == "identifier"
                and attr.text == b"register"
            ):
                positional_args = [c for c in arg_list.children if c.type not in ("(", ")", ",")]
                if len(positional_args) >= 2:
                    kind = _string_value(positional_args[0])
                    name = _extract_name_from_struct_call(positional_args[1])
                    if kind is not None and name is not None:
                        return [(kind, name)]
        return []

    # Helper form: root("foo", ...) / task(name="foo", ...) etc.
    if func_node.type == "identifier":
        func_name = func_node.text
        if func_name is None:
            return []
        decoded_name = func_name.decode()
        if decoded_name == "cached_value":
            return _cached_value_entries(arg_list)
        kind = _HELPER_KINDS.get(decoded_name)
        if kind is None:
            return []
        # Try positional string first, then keyword name= argument.
        name = _first_positional_string(arg_list)
        if name is None:
            name = _keyword_arg_name(arg_list)
        if name is not None:
            return [(kind, name)]

    return []


def _walk_node(
    node: tree_sitter.Node,  # type: ignore[type-arg]
    result: dict[tuple[str, str], tuple[int, int]],
    file_path: Path,
) -> None:
    """Recursively walk *node*, collecting rule-call ranges into *result*.

    Nodes with ``type == "ERROR"`` are skipped entirely (along with their
    subtree) because their children may be misattributed tokens.  Nodes that
    merely *contain* errors (``has_error == True``) are still descended into
    so that valid siblings of the broken subtree are not lost.
    """
    if node.type == "ERROR":
        return  # skip broken subtree entirely; has_error on parents is propagated

    if node.type == "call":
        entries = _process_call(node)
        for entry in entries:
            start_line = node.start_point[0] + 1  # row is 0-based
            end_line = node.end_point[0] + 1
            if entry in result:
                if entry[0] == "value":
                    result[entry] = (start_line, end_line)  # last-write-wins: inline value() inside task/action
                else:
                    existing = result[entry]
                    raise ValueError(
                        f"Duplicate ({entry[0]!r}, {entry[1]!r}) in {file_path}: "
                        f"first at lines {existing[0]}-{existing[1]}, "
                        f"second at lines {start_line}-{end_line}"
                    )
            else:
                result[entry] = (start_line, end_line)

    for child in node.children:
        _walk_node(child, result, file_path)


def extract_entity_ranges(
    file_path: Path, source: str
) -> dict[tuple[str, str], tuple[int, int]]:
    """Return ``{(kind, name): (start_line, end_line)}`` — 1-based, inclusive.

    Parses *source* with tree-sitter-starlark and walks the full AST,
    visiting every node at any depth. Registration calls nested inside
    function bodies or as inline arguments are captured alongside top-level
    calls.

    Only registration calls with literal string names are included; calls
    with computed names are silently skipped. Duplicate ``(kind, name)`` pairs
    within the same file raise ``ValueError``. Subtrees rooted at ERROR nodes
    are skipped entirely.
    """
    tree = _parser.parse(source.encode())
    result: dict[tuple[str, str], tuple[int, int]] = {}
    _walk_node(tree.root_node, result, file_path)
    return result
