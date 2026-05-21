"""Pure-syntactic parser for mlody label strings.

ABNF grammar (Appendix C of REQUIREMENTS.md):

    label           = [workspace_spec "|"] [entity_spec] ["'" attribute_path]

    workspace_spec  = workspace_name ["[" query_body "]"]
    workspace_name  = *VCHAR                ; everything before "|" or "[" or "'"

    entity_spec     = ["@" root_name] "//" path_spec [":" entity_field_path]
                      ["[" query_body "]"]
    root_name       = 1*name_char
    path_spec       = path_segments ["/" "..."]
    path_segments   = path_component *("/" path_component)
    path_component  = 1*name_char
    entity_field_path = entity_name *("." entity_name)
                      ; first segment is the entity name; subsequent segments
                      ; are stored in EntitySpec.field_path for struct traversal
    entity_name     = 1*name_char
    name_char       = ALPHA / DIGIT / "-" / "_" / "."

    attribute_path  = attr_segment *("." attr_segment) ["[" query_body "]"]
    attr_segment    = 1*name_char

    query_body      = *VCHAR               ; stored verbatim, not validated

Disambiguation rules (applied in this fixed order when "|" is absent):
  1. "|" present -> left is workspace_spec, right is [entity_spec]["'"attr_path]
  2. Starts with "//" or "@" -> workspace=None, full string is entity+attr
  3. Otherwise -> everything before "'" (or whole string) is workspace_spec
"""

from __future__ import annotations

from mlody.core.label.errors import (
    AttributeParseError,
    EntityParseError,
    LabelParseError,
    WorkspaceParseError,
)
from mlody.core.label.label import EntitySpec, Label


def extract_sql_query(entity_query: str | None) -> tuple[str, str] | None:
    """Extract a SQL dialect tag and fragment from an entity query suffix.

    Returns ``("duckdb", fragment)`` when ``entity_query`` starts with the
    literal prefix ``"@sql "`` (case-sensitive, one trailing space required),
    stripping that prefix to produce the fragment.  Returns ``None`` for any
    other input, including ``None`` itself.

    This is a post-parse helper — it does not modify the parser or the raw
    ``entity_query`` field on ``Label``/``EntitySpec``.

    Args:
        entity_query: The verbatim bracket content from a parsed label, or
            ``None`` if no bracket suffix was present.

    Returns:
        ``(dialect, sql_fragment)`` or ``None``.
    """
    _SQL_PREFIX = "@sql "
    if entity_query is None:
        return None
    if not entity_query.startswith(_SQL_PREFIX):
        return None
    fragment = entity_query[len(_SQL_PREFIX):]
    return ("duckdb", fragment)


def _strip_query(fragment: str) -> tuple[str, str | None]:
    """Return ``(body, query_content)`` after removing a trailing ``[...]``.

    A trailing bracket is one whose closing ``]`` is the last character.
    When the fragment ends with ``]``, scan right-to-left to find its matching
    ``[`` and strip that pair as the query suffix.

    When the fragment does *not* end with ``]`` but contains ``[``, the brackets
    are inline (e.g. ``valid[1:4].Bald``).  In that case the fragment is
    returned unchanged with ``query=None``.  A ``ValueError`` is raised only
    when a ``[`` has no matching ``]`` anywhere in the fragment (truly unclosed).
    """
    if "[" not in fragment:
        return fragment, None
    if fragment.endswith("]"):
        depth = 0
        for i in range(len(fragment) - 1, -1, -1):
            if fragment[i] == "]":
                depth += 1
            elif fragment[i] == "[":
                depth -= 1
                if depth == 0:
                    return fragment[:i], fragment[i + 1 : -1]
        raise ValueError(f"unmatched ']' in {fragment!r}")
    # Inline brackets — verify they are balanced.
    depth = 0
    for ch in fragment:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    if depth != 0:
        raise ValueError(f"unclosed '[' in {fragment!r}")
    return fragment, None


def _find_tick_outside_brackets(s: str) -> int:
    """Return the index of the first ``'`` not inside ``[...]``, or -1."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == "[":
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
        elif ch == "'" and depth == 0:
            return i
    return -1


def _parse_workspace_fragment(
    raw: str, fragment: str
) -> tuple[str | None, str | None]:
    """Parse the workspace section and return ``(workspace, workspace_query)``.

    An empty fragment means the workspace was omitted (e.g. ``|//foo``); both
    fields are ``None``.  Otherwise the fragment is passed through
    ``_strip_query`` and the remainder is the workspace name.
    """
    if not fragment:
        return None, None
    try:
        body, query = _strip_query(fragment)
    except ValueError as exc:
        raise WorkspaceParseError(
            raw,
            str(exc),
            workspace_fragment=fragment,
        ) from exc
    return body or None, query


def _parse_entity_fragment(raw: str, fragment: str) -> tuple[EntitySpec, str | None]:
    """Parse the entity section and return ``(EntitySpec, entity_query)``.

    Raises ``EntityParseError`` for any structural violation.
    """
    try:
        body, query = _strip_query(fragment)
    except ValueError as exc:
        raise EntityParseError(
            raw,
            str(exc),
            entity_fragment=fragment,
        ) from exc

    if query is not None and body.endswith("/"):
        body = body[:-1]

    remainder = body

    # Optional @root prefix in "@root//path" or bare "@root" form
    root: str | None = None
    if remainder.startswith("@"):
        at_end = remainder.find("//")
        if at_end == -1:
            # Bare root reference: "@lexica" with no path — valid on its own.
            root = remainder[1:]
            if not root:
                raise EntityParseError(
                    raw,
                    "'@' must be followed by a root name",
                    entity_fragment=fragment,
                )
            return EntitySpec(root=root, path=None, wildcard=False, name=None, field_path=None), query
        root = remainder[1:at_end]
        remainder = remainder[at_end:]

    # Require //
    if not remainder.startswith("//"):
        raise EntityParseError(
            raw,
            "entity must start with '//'",
            entity_fragment=fragment,
        )
    remainder = remainder[2:]  # strip leading "//"

    # After stripping //, a bare "@" means the user wrote "//@root/path"
    # instead of the correct "@root//path" form.
    if remainder.startswith("@"):
        raise EntityParseError(
            raw,
            "use '@root//path' form, not '//@root/path'",
            entity_fragment=fragment,
        )

    # Optional :entity_field_path suffix (before wildcard check).
    # The part after ":" is split on the first "." to separate the entity name
    # from an optional struct traversal path (e.g. ":pretrain.outputs.weights"
    # → name="pretrain", field_path=("outputs", "weights")).
    name: str | None = None
    field_path_tuple: tuple[str, ...] | None = None
    colon_pos = remainder.find(":")
    if colon_pos != -1:
        name_part = remainder[colon_pos + 1 :]
        if not name_part and query is None:
            raise EntityParseError(
                raw,
                "entity name after ':' must not be empty",
                entity_fragment=fragment,
            )
        if name_part:
            if query is None and name_part.startswith("["):
                depth = 0
                quote: str | None = None
                escaped = False
                close_idx: int | None = None
                for i, ch in enumerate(name_part):
                    if quote is not None:
                        if escaped:
                            escaped = False
                        elif ch == "\\":
                            escaped = True
                        elif ch == quote:
                            quote = None
                        continue
                    if ch in ("'", '"'):
                        quote = ch
                        continue
                    if ch == "[":
                        depth += 1
                        continue
                    if ch == "]":
                        depth -= 1
                        if depth == 0:
                            close_idx = i
                            break
                if close_idx is None:
                    raise EntityParseError(
                        raw,
                        f"unclosed '[' in {fragment!r}",
                        entity_fragment=fragment,
                    )
                query = name_part[1:close_idx]
                suffix = name_part[close_idx + 1 :]
                if suffix:
                    if not suffix.startswith("."):
                        raise EntityParseError(
                            raw,
                            "entity field path after query must start with '.'",
                            entity_fragment=fragment,
                        )
                    field_suffix = suffix[1:]
                    if not field_suffix or field_suffix.startswith(".") or field_suffix.endswith("."):
                        raise EntityParseError(
                            raw,
                            "entity field path after query must not be empty",
                            entity_fragment=fragment,
                        )
                    field_path_tuple = tuple(field_suffix.split("."))
            else:
                dot_idx = name_part.find(".")
                if dot_idx != -1:
                    name = name_part[:dot_idx]
                    field_path_tuple = tuple(name_part[dot_idx + 1 :].split("."))
                else:
                    name = name_part
                    field_path_tuple = None
        remainder = remainder[:colon_pos]

    # Wildcard check: path ends with /... or is just ...
    wildcard = False
    if remainder.endswith("/..."):
        wildcard = True
        remainder = remainder[:-4]  # strip "/..."
    elif remainder == "...":
        # //... — wildcard with no path prefix: search everywhere under the root
        wildcard = True
        remainder = ""

    path: str | None = remainder if remainder else None
    if not wildcard and path is None:
        raise EntityParseError(
            raw,
            "entity path must not be empty after '//'",
            entity_fragment=fragment,
        )

    return (
        EntitySpec(root=root, path=path, wildcard=wildcard, name=name, field_path=field_path_tuple),
        query,
    )


def _parse_attribute_fragment(
    raw: str, fragment: str
) -> tuple[tuple[str, ...], str | None]:
    """Parse the attribute section and return ``(path_tuple, attribute_query)``.

    Raises ``AttributeParseError`` for empty segments or unclosed brackets.
    """
    try:
        body, query = _strip_query(fragment)
    except ValueError as exc:
        raise AttributeParseError(
            raw,
            str(exc),
            attribute_fragment=fragment,
        ) from exc

    if body.endswith("."):
        raise AttributeParseError(
            raw,
            "attribute path must not end with '.'",
            attribute_fragment=fragment,
        )

    segments = body.split(".")
    for seg in segments:
        if not seg:
            raise AttributeParseError(
                raw,
                "attribute path segments must not be empty",
                attribute_fragment=fragment,
            )

    return tuple(segments), query


def parse_label(raw: str) -> Label:
    """Parse a raw label string into a structured ``Label``.

    Applies the three disambiguation rules in order to determine which part of
    the string is the workspace spec, entity spec, and attribute path.

    Raises ``LabelParseError`` (or a typed subclass) on any syntax violation.
    """
    if not raw:
        raise LabelParseError(raw, "label must not be empty")

    # -- Rule 1: pipe present -------------------------------------------------
    # A "|" that is not inside brackets splits workspace from entity+attr.
    pipe_pos = -1
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "[":
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
        elif ch == "|" and depth == 0:
            pipe_pos = i
            break

    if pipe_pos != -1:
        ws_fragment = raw[:pipe_pos]
        rest = raw[pipe_pos + 1 :]
        workspace, workspace_query = _parse_workspace_fragment(raw, ws_fragment)

        tick_pos = _find_tick_outside_brackets(rest)
        if tick_pos != -1:
            entity_fragment = rest[:tick_pos]
            attr_fragment = rest[tick_pos + 1 :]
        else:
            entity_fragment = rest
            attr_fragment = ""

        entity: EntitySpec | None = None
        entity_query: str | None = None
        attribute_path: tuple[str, ...] | None = None
        attribute_query: str | None = None

        if entity_fragment:
            entity, entity_query = _parse_entity_fragment(raw, entity_fragment)
        if attr_fragment:
            attribute_path, attribute_query = _parse_attribute_fragment(
                raw, attr_fragment
            )

        if workspace is None and entity is None and attribute_path is None:
            raise LabelParseError(raw, "label has no content")

        return Label(
            workspace=workspace,
            workspace_query=workspace_query,
            entity=entity,
            entity_query=entity_query,
            attribute_path=attribute_path,
            attribute_query=attribute_query,
        )

    # -- Rule 2: no pipe, starts with "//" or "@" -----------------------------
    if raw.startswith("//") or raw.startswith("@"):
        tick_pos = _find_tick_outside_brackets(raw)
        if tick_pos != -1:
            entity_fragment = raw[:tick_pos]
            attr_fragment = raw[tick_pos + 1 :]
        else:
            entity_fragment = raw
            attr_fragment = ""

        entity, entity_query = _parse_entity_fragment(raw, entity_fragment)

        attribute_path = None
        attribute_query = None
        if attr_fragment:
            attribute_path, attribute_query = _parse_attribute_fragment(
                raw, attr_fragment
            )

        return Label(
            workspace=None,
            workspace_query=None,
            entity=entity,
            entity_query=entity_query,
            attribute_path=attribute_path,
            attribute_query=attribute_query,
        )

    # -- Rule 3: no pipe, no "//"/"@" -----------------------------------------
    # Everything before the first "'" is the workspace; after is the attribute.
    tick_pos = _find_tick_outside_brackets(raw)
    if tick_pos != -1:
        ws_fragment = raw[:tick_pos]
        attr_fragment = raw[tick_pos + 1 :]
    else:
        ws_fragment = raw
        attr_fragment = ""

    workspace, workspace_query = _parse_workspace_fragment(raw, ws_fragment)

    attribute_path = None
    attribute_query = None
    if attr_fragment:
        attribute_path, attribute_query = _parse_attribute_fragment(
            raw, attr_fragment
        )

    return Label(
        workspace=workspace,
        workspace_query=workspace_query,
        entity=None,
        entity_query=None,
        attribute_path=attribute_path,
        attribute_query=attribute_query,
    )
