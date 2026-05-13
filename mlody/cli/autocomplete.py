"""Autocomplete helpers for the stage label input."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from common.python.starlarkish.core.struct import Struct
from mlody.core.label import parse_label
from mlody.core.workspace import force
from mlody.core.workspace_models import RootInfo
from mlody.resolver import (
    MlodyActionValue,
    MlodyTaskValue,
    MlodyUnresolvedValue,
    MlodyValueValue,
    resolve_label_to_value,
)
from mlody.resolver.label_value import _RawAttrValue

_IDENTIFIER_EXTRA_CHARS = frozenset({"_", "-"})
_UNSUPPORTED_FRAGMENT_CHARS = frozenset({"[", "]", "'", "|"})


@dataclass(frozen=True)
class StageAutocompleteRequest:
    """Validated autocomplete request payload from the stage frontend."""

    workspace_root: str | None
    breadcrumb: tuple[str, ...]
    prompt: str


@dataclass(frozen=True)
class _ParsedBreadcrumb:
    root: str | None
    has_scope_separator: bool
    package_segments: tuple[str, ...]
    colon_seen: bool
    entity_segments: tuple[str, ...]


@dataclass(frozen=True)
class _CompletionContext:
    kind: str
    root: str | None
    package_segments: tuple[str, ...]
    entity_segments: tuple[str, ...]
    prefix: str


def parse_stage_autocomplete_request(payload: object) -> StageAutocompleteRequest:
    """Validate the stage autocomplete HTTP payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("Request body must be a JSON object.")

    workspace_root = payload.get("workspaceRoot")
    if workspace_root is not None and not isinstance(workspace_root, str):
        raise ValueError("Field 'workspaceRoot' must be a string or null.")

    breadcrumb = payload.get("breadcrumb", [])
    if not isinstance(breadcrumb, Sequence) or isinstance(breadcrumb, (str, bytes)):
        raise ValueError("Field 'breadcrumb' must be an array of strings.")
    breadcrumb_values = list(breadcrumb)
    if not all(isinstance(item, str) for item in breadcrumb_values):
        raise ValueError("Field 'breadcrumb' must be an array of strings.")

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("Field 'prompt' must be a string.")

    return StageAutocompleteRequest(
        workspace_root=workspace_root,
        breadcrumb=tuple(breadcrumb_values),
        prompt=prompt,
    )


def stage_label_completions(
    workspace: object,
    breadcrumb: Sequence[str],
    prompt: str,
) -> list[str]:
    """Return stage label completion candidates for the current editor state."""

    context = _detect_completion_context(breadcrumb, prompt)
    if context is None:
        return []

    if context.kind == "root":
        return _root_completions(workspace, context.prefix)
    if context.kind == "package":
        return _package_completions(
            workspace,
            root=context.root,
            package_segments=context.package_segments,
            prefix=context.prefix,
        )
    if context.kind == "target":
        return _target_completions(
            workspace,
            root=context.root,
            package_segments=context.package_segments,
            prefix=context.prefix,
        )
    if context.kind == "field":
        return _field_completions(
            workspace,
            root=context.root,
            package_segments=context.package_segments,
            entity_segments=context.entity_segments,
            prefix=context.prefix,
        )

    return []


def stage_autocomplete_payload(
    workspace: object,
    breadcrumb: Sequence[str],
    prompt: str,
) -> dict[str, object]:
    """Build the JSON payload returned to the stage frontend."""

    return {
        "completions": stage_label_completions(workspace, breadcrumb, prompt),
        "additionalData": {},
    }


def _detect_completion_context(
    breadcrumb: Sequence[str],
    prompt: str,
) -> _CompletionContext | None:
    parsed = _parse_breadcrumb(breadcrumb)
    if parsed is None:
        return None

    if prompt == "" and len(breadcrumb) == 0:
        return None
    if _has_unsupported_fragment(prompt):
        return None
    if "\n" in prompt or "\r" in prompt:
        return None

    if prompt.startswith("@"):
        if len(breadcrumb) > 0 or ":" in prompt or "/" in prompt or "." in prompt:
            return None
        prefix = prompt[1:]
        if not _is_identifier_prefix(prefix):
            return None
        return _CompletionContext(
            kind="root",
            root=None,
            package_segments=(),
            entity_segments=(),
            prefix=prefix,
        )

    if prompt.startswith("."):
        if not parsed.colon_seen or len(parsed.entity_segments) == 0:
            return None
        if "/" in prompt or ":" in prompt:
            return None
        prefix = prompt[1:]
        if not _is_identifier_prefix(prefix):
            return None
        return _CompletionContext(
            kind="field",
            root=parsed.root,
            package_segments=parsed.package_segments,
            entity_segments=parsed.entity_segments,
            prefix=prefix,
        )

    if ":" in prompt or "/" in prompt or "." in prompt:
        return None
    if not _is_identifier_prefix(prompt):
        return None

    if parsed.colon_seen:
        if len(parsed.entity_segments) > 0:
            return None
        return _CompletionContext(
            kind="target",
            root=parsed.root,
            package_segments=parsed.package_segments,
            entity_segments=(),
            prefix=prompt,
        )

    if not parsed.has_scope_separator:
        return None

    return _CompletionContext(
        kind="package",
        root=parsed.root,
        package_segments=parsed.package_segments,
        entity_segments=(),
        prefix=prompt,
    )


def _parse_breadcrumb(breadcrumb: Sequence[str]) -> _ParsedBreadcrumb | None:
    remaining = list(breadcrumb)
    root: str | None = None
    if remaining and remaining[0].startswith("@"):
        root_segment = remaining.pop(0)
        if not _is_root_segment(root_segment):
            return None
        root = root_segment[1:]

    has_scope_separator = False
    if remaining and remaining[0] == "//":
        has_scope_separator = True
        remaining.pop(0)

    package_segments: list[str] = []
    entity_segments: list[str] = []
    colon_seen = False

    for raw_segment in remaining:
        if raw_segment == "//":
            if has_scope_separator or package_segments or entity_segments or colon_seen:
                return None
            has_scope_separator = True
            continue

        if not raw_segment or _has_unsupported_fragment(raw_segment):
            return None
        if "/" in raw_segment or raw_segment.startswith(".") or raw_segment.startswith("@"):
            return None
        if raw_segment in {"...", "...:"}:
            return None

        if raw_segment.endswith(":"):
            if colon_seen:
                return None
            segment = raw_segment[:-1]
            if not _is_identifier_prefix(segment):
                return None
            package_segments.append(segment)
            colon_seen = True
            has_scope_separator = True
            continue

        if not _is_identifier_prefix(raw_segment):
            return None

        if colon_seen:
            entity_segments.append(raw_segment)
        else:
            package_segments.append(raw_segment)
        has_scope_separator = True

    return _ParsedBreadcrumb(
        root=root,
        has_scope_separator=has_scope_separator,
        package_segments=tuple(package_segments),
        colon_seen=colon_seen,
        entity_segments=tuple(entity_segments),
    )


def _has_unsupported_fragment(value: str) -> bool:
    return any(character in _UNSUPPORTED_FRAGMENT_CHARS for character in value)


def _is_identifier_prefix(value: str) -> bool:
    return all(character.isalnum() or character in _IDENTIFIER_EXTRA_CHARS for character in value)


def _is_root_segment(value: str) -> bool:
    return value.startswith("@") and _is_identifier_prefix(value[1:]) and value != "@"


def _root_completions(workspace: object, prefix: str) -> list[str]:
    root_infos = getattr(workspace, "root_infos", {})
    if not isinstance(root_infos, Mapping):
        return []
    return _sorted_unique(
        root_name
        for root_name in root_infos
        if isinstance(root_name, str) and root_name.startswith(prefix)
    )


def _package_completions(
    workspace: object,
    *,
    root: str | None,
    package_segments: Sequence[str],
    prefix: str,
) -> list[str]:
    suggestions: list[str] = []
    for rel_stem, _name in _iter_relative_registry_entries(workspace, root=root):
        rel_segments = _split_stem_segments(rel_stem)
        if len(rel_segments) <= len(package_segments):
            continue
        if tuple(rel_segments[: len(package_segments)]) != tuple(package_segments):
            continue
        candidate = rel_segments[len(package_segments)]
        if candidate.startswith(prefix):
            suggestions.append(candidate)
    return _sorted_unique(suggestions)


def _target_completions(
    workspace: object,
    *,
    root: str | None,
    package_segments: Sequence[str],
    prefix: str,
) -> list[str]:
    package_path = "/".join(package_segments)
    suggestions: list[str] = []
    for rel_stem, name in _iter_relative_registry_entries(workspace, root=root):
        if rel_stem != package_path:
            continue
        if name.startswith(prefix):
            suggestions.append(name)
    return _sorted_unique(suggestions)


def _field_completions(
    workspace: object,
    *,
    root: str | None,
    package_segments: Sequence[str],
    entity_segments: Sequence[str],
    prefix: str,
) -> list[str]:
    parent_label = _render_entity_label(root, package_segments, entity_segments)
    try:
        parsed_label = parse_label(parent_label)
    except ValueError:
        return []

    resolved = resolve_label_to_value(parsed_label, workspace)
    if isinstance(resolved, MlodyUnresolvedValue):
        return []

    structured = _unwrap_structured_value(resolved)
    if isinstance(structured, Struct):
        return _sorted_unique(
            name
            for name in structured.as_mapping()
            if isinstance(name, str) and name.startswith(prefix)
        )
    if isinstance(structured, Mapping):
        return _sorted_unique(
            str(name)
            for name in structured
            if isinstance(name, str) and name.startswith(prefix)
        )
    return []


def _unwrap_structured_value(value: object) -> object:
    current = value
    while True:
        if isinstance(current, (MlodyTaskValue, MlodyActionValue, MlodyValueValue)):
            current = current.struct
            continue
        if isinstance(current, _RawAttrValue):
            current = current.value
            continue
        break

    try:
        return force(current)
    except Exception:
        return current


def _iter_relative_registry_entries(
    workspace: object,
    *,
    root: str | None,
) -> tuple[tuple[str, str], ...]:
    registry_view = getattr(workspace, "registry_view", None)
    iter_registry_items = getattr(registry_view, "iter_registry_items", None)
    if not callable(iter_registry_items):
        return ()

    entries: list[tuple[str, str]] = []
    root_prefix = _registry_stem_prefix(workspace, root=root)
    if root_prefix is None and root is not None:
        return ()

    for key, _value in iter_registry_items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        raw_stem, raw_name = key[1], key[2]
        if not isinstance(raw_stem, str) or not isinstance(raw_name, str):
            continue

        rel_stem = _stem_relative_to_prefix(raw_stem, root_prefix)
        if rel_stem is None:
            continue
        entries.append((rel_stem, raw_name))

    return tuple(entries)


def _registry_stem_prefix(workspace: object, *, root: str | None) -> str | None:
    if root is not None:
        root_infos = getattr(workspace, "root_infos", {})
        if not isinstance(root_infos, Mapping):
            return None
        raw_root_info = root_infos.get(root)
        if isinstance(raw_root_info, RootInfo):
            return raw_root_info.path.lstrip("/").rstrip("/")
        path = getattr(raw_root_info, "path", None)
        if isinstance(path, str):
            return path.lstrip("/").rstrip("/")
        return None

    monorepo_root = getattr(workspace, "_monorepo_root", None)
    workspace_root = getattr(workspace, "_workspace_root", None)
    if not isinstance(monorepo_root, Path) or not isinstance(workspace_root, Path):
        return ""
    if workspace_root == monorepo_root:
        return ""

    try:
        return workspace_root.relative_to(monorepo_root).as_posix().strip("/")
    except ValueError:
        return ""


def _stem_relative_to_prefix(raw_stem: str, prefix: str | None) -> str | None:
    normalized_stem = raw_stem.strip("/")
    if prefix is None:
        return None
    normalized_prefix = prefix.strip("/")
    if normalized_prefix == "":
        return normalized_stem
    if normalized_stem == normalized_prefix:
        return ""
    prefix_with_sep = f"{normalized_prefix}/"
    if not normalized_stem.startswith(prefix_with_sep):
        return None
    return normalized_stem[len(prefix_with_sep) :]


def _split_stem_segments(stem: str) -> tuple[str, ...]:
    if stem == "":
        return ()
    return tuple(segment for segment in stem.split("/") if segment)


def _render_entity_label(
    root: str | None,
    package_segments: Sequence[str],
    entity_segments: Sequence[str],
) -> str:
    parts: list[str] = []
    if root is not None:
        parts.append(f"@{root}")

    package_path = "/".join(package_segments)
    if package_path:
        parts.append(f"//{package_path}")
    elif root is None:
        parts.append("//")

    if entity_segments:
        parts.append(f":{entity_segments[0]}")
        if len(entity_segments) > 1:
            parts.append(f".{'.'.join(entity_segments[1:])}")

    return "".join(parts)


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})
