"""Target address parsing and resolution for Bazel-style target references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TargetAddress:
    """Parsed Bazel-style target address.

    Format: @ROOT//package/path:target_name.field.subfield
    """

    root: str | None
    package_path: str | None
    target_name: str
    field_path: tuple[str, ...]


def parse_target(raw: str) -> TargetAddress:
    """Parse a target string into a TargetAddress.

    Supported formats:
        @ROOT//package/path:target_name.field.subfield
        //package/path:target_name
        :target_name.field

    Raises:
        ValueError: On malformed input.
    """
    if not raw:
        msg = "Target string is empty"
        raise ValueError(msg)

    rest = raw
    root: str | None = None
    package_path: str | None = None

    # Extract @ROOT prefix
    if rest.startswith("@"):
        slash_idx = rest.find("//")
        if slash_idx == -1:
            msg = f"Invalid target syntax: expected '//' after root in {raw!r}"
            raise ValueError(msg)
        root = rest[1:slash_idx]
        rest = rest[slash_idx:]

    # Extract //package_path
    if rest.startswith("//"):
        rest = rest[2:]
        colon_idx = rest.find(":")
        if colon_idx == -1:
            msg = f"Invalid target syntax: missing ':' separator in {raw!r}"
            raise ValueError(msg)
        package_path = rest[:colon_idx]
        rest = rest[colon_idx:]

    # Must start with ':'
    if not rest.startswith(":"):
        msg = f"Invalid target syntax: missing ':' separator in {raw!r}"
        raise ValueError(msg)

    rest = rest[1:]  # strip ':'

    # Split target_name from field_path on first '.'
    if "." in rest:
        dot_idx = rest.index(".")
        target_name = rest[:dot_idx]
        field_path = tuple(rest[dot_idx + 1 :].split("."))
    else:
        target_name = rest
        field_path = ()

    if not target_name:
        msg = f"Target name is empty in {raw!r}"
        raise ValueError(msg)

    return TargetAddress(
        root=root,
        package_path=package_path,
        target_name=target_name,
        field_path=field_path,
    )


def resolve_target_value(
    address: TargetAddress,
    roots: dict[str, Any],
) -> object:
    """Resolve a TargetAddress against a roots dictionary.

    Traverses roots dict -> root object -> target_name -> field_path
    using getattr() for Struct field access.

    Raises:
        KeyError: If root or target is not found.
        AttributeError: If a field in field_path does not exist.
    """
    if address.root is None:
        msg = f"No root specified in target address; available roots: {sorted(roots)}"
        raise KeyError(msg)

    if address.root not in roots:
        msg = f"Root {address.root!r} not found; available roots: {sorted(roots)}"
        raise KeyError(msg)

    obj: object = roots[address.root]

    # Navigate to target_name via getattr
    obj = getattr(obj, address.target_name)

    # Traverse field_path
    for field in address.field_path:
        obj = getattr(obj, field)

    return obj
