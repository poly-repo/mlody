"""Shared typed location-spec adapters for runtime values and assets."""

from __future__ import annotations

import glob
import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable

from mlody.common.struct import Struct, is_struct_like, struct_like_as_mapping
from mlody.core.query_spec import QuerySpec


def _specific_kind(location: object) -> str:
    """Return the most specific location discriminator available."""
    return (
        getattr(location, "_root_kind", None)
        or getattr(location, "type", None)
        or getattr(location, "kind", "")
    )


def _coerce_path_tuple(path_value: object) -> tuple[str, ...]:
    """Convert a location path payload to a tuple of strings."""
    if path_value is None:
        return ()
    if isinstance(path_value, str):
        return (path_value,)
    if isinstance(path_value, Path):
        return (str(path_value),)
    if isinstance(path_value, (list, tuple)):
        return tuple(str(path) for path in path_value)
    return (str(path_value),)


def _paths_from_location(location: object) -> tuple[str, ...]:
    """Extract path strings from either direct fields or ``attributes``."""
    direct = getattr(location, "path", None)
    if direct is not None:
        return _coerce_path_tuple(direct)
    attrs = getattr(location, "attributes", None)
    if isinstance(attrs, dict):
        return _coerce_path_tuple(attrs.get("path"))
    if is_struct_like(attrs):
        return _coerce_path_tuple(struct_like_as_mapping(attrs).get("path"))
    return ()


def _location_attributes(location: object) -> dict[str, object]:
    """Return a location's attributes payload regardless of mapping shape."""
    attrs = getattr(location, "attributes", None)
    if isinstance(attrs, dict):
        return dict(attrs)
    if is_struct_like(attrs):
        return dict(struct_like_as_mapping(attrs))
    return {}


def _source_value_struct(value_struct: object) -> object | None:
    """Return the embedded source value struct when available."""
    source_value = getattr(value_struct, "_source_value", None)
    if source_value is not None:
        return source_value
    source_attr = getattr(value_struct, "source", None)
    if getattr(source_attr, "kind", None) == "value":
        return source_attr
    return None


def _expand_pattern(path_pattern: str) -> tuple[str, ...]:
    """Expand a filesystem glob pattern, preserving unmatched literals."""
    expanded_pattern = os.path.expanduser(path_pattern)
    matches = tuple(sorted(glob.glob(expanded_pattern)))
    return matches or (path_pattern,)


def _dedupe_preserving_order(paths: Iterable[str]) -> tuple[str, ...]:
    """Return a stable-order tuple with duplicates removed."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return tuple(unique)


def _derived_cache_root() -> Path:
    """Return the writable cache root for derived parquet outputs."""
    test_tmpdir = os.environ.get("TEST_TMPDIR")
    if test_tmpdir:
        return Path(test_tmpdir) / "mlody" / "derived"
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "mlody" / "derived"
    return Path.home() / ".cache" / "mlody" / "derived"


def _derived_cache_path(source_paths: tuple[str, ...], query: QuerySpec) -> Path:
    """Return the deterministic cache path used for composed derived values."""
    raw_key = ":".join(sorted(source_paths)) + ":" + query.dialect + ":" + query.sql
    digest = hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    return _derived_cache_root() / f"{digest}.parquet"


@dataclass(frozen=True)
class PosixLocationSpec:
    """Typed view of a path-backed location used by the runtime."""

    paths: tuple[str, ...]
    name: str = ""
    kind: str = "posix"

    @classmethod
    def from_location(cls, location: object) -> PosixLocationSpec | None:
        """Parse a runtime location object into a typed posix spec."""
        if location is None or _specific_kind(location) == "derived":
            return None
        paths = _paths_from_location(location)
        if not paths:
            return None
        kind = _specific_kind(location) or "posix"
        return cls(paths=paths, name=str(getattr(location, "name", "")), kind=kind)

    def compose(
        self,
        field_spec: PosixLocationSpec | None,
        field_name: str,
    ) -> PosixLocationSpec:
        """Compose this parent path set with a field path set or field name."""
        field_paths = field_spec.paths if field_spec is not None else (field_name,)
        if not field_paths:
            field_paths = (field_name,)

        composed_patterns = tuple(
            os.path.join(parent_path, field_path)
            for parent_path in (self.paths or ("",))
            for field_path in field_paths
        )
        expanded_paths = _dedupe_preserving_order(
            expanded
            for pattern in composed_patterns
            for expanded in _expand_pattern(pattern)
        )
        return PosixLocationSpec(
            paths=expanded_paths or composed_patterns,
            name=self.name,
            kind="posix",
        )

    def to_struct(self) -> Struct:
        """Serialize the typed spec back into the runtime Struct shape."""
        return Struct(
            kind="location",
            type="posix",
            name=self.name,
            path=list(self.paths),
        )


@dataclass(frozen=True)
class RemoteLocationSpec:
    """Typed view of an HTTP-backed remote location."""

    uri: str
    name: str = "remote"

    @classmethod
    def from_location(cls, location: object) -> RemoteLocationSpec | None:
        """Parse a runtime location object into a typed HTTP-backed spec."""
        kind = _specific_kind(location)
        if location is None or kind not in {"remote", "https"}:
            return None
        uri = getattr(location, "uri", None)
        if uri is None:
            uri = _location_attributes(location).get("uri")
        if not isinstance(uri, str) or uri == "":
            return None
        default_name = kind if kind in {"remote", "https"} else "https"
        return cls(uri=uri, name=str(getattr(location, "name", default_name) or default_name))


@dataclass(frozen=True)
class SshLocationSpec:
    """Typed view of an SSH-backed artifact staged in the local cache."""

    host: str
    path: str
    name: str = "ssh"

    @classmethod
    def from_location(cls, location: object) -> SshLocationSpec | None:
        """Parse a runtime location object into a typed SSH spec."""
        if location is None or _specific_kind(location) != "ssh":
            return None
        host = getattr(location, "host", None)
        path = getattr(location, "path", None)
        attrs = _location_attributes(location)
        if host is None:
            host = attrs.get("host")
        if path is None:
            path = attrs.get("path")
        if not isinstance(host, str) or host == "":
            return None
        if not isinstance(path, str) or path == "":
            return None
        return cls(host=host, path=path, name=str(getattr(location, "name", "ssh") or "ssh"))

    def cache_relative_path(self) -> PurePosixPath:
        """Return the cache-relative path for the staged SSH artifact."""
        raw_path = self.path[1:] if self.path.startswith("/") else self.path
        parts = tuple(
            part for part in PurePosixPath(raw_path).parts
            if part not in {"", "."}
        )
        if not parts:
            raise ValueError("ssh(path=...) must reference a concrete file path")
        if any(part == ".." for part in parts):
            raise ValueError("ssh(path=...) may not contain '..'")
        return PurePosixPath(*parts)

    def cache_path(self, *, cache_root: Path | None = None) -> Path:
        """Return the local cache path used for this SSH-backed artifact."""
        root = (
            cache_root
            if cache_root is not None
            else Path.home() / ".cache" / "mlody" / "remotes"
        )
        return root / self.host / self.cache_relative_path()


@dataclass(frozen=True)
class DerivedLocationSpec:
    """Typed view of a derived location backed by a SQL query over parquet."""

    source_ref: str
    source_paths: tuple[str, ...]
    query: QuerySpec
    output_path: Path
    name: str = "derived"

    @classmethod
    def from_location(cls, location: object) -> DerivedLocationSpec | None:
        """Parse a derived runtime location object into a typed spec."""
        if location is None or _specific_kind(location) != "derived":
            return None
        attrs = _location_attributes(location)
        query = QuerySpec(
            sql=str(attrs.get("sql_fragment") or ""),
            dialect=str(attrs.get("dialect") or "duckdb"),
        )
        output_value = attrs.get("output_path")
        source_paths = _coerce_path_tuple(attrs.get("source_paths"))
        output_path = Path(str(output_value)) if output_value else _derived_cache_path(
            source_paths,
            query,
        )
        return cls(
            source_ref=str(attrs.get("source_ref") or ""),
            source_paths=source_paths,
            query=query,
            output_path=output_path,
            name=str(getattr(location, "name", "derived") or "derived"),
        )

    def with_source_paths(self, source_paths: tuple[str, ...]) -> DerivedLocationSpec:
        """Return the same derived spec with explicit resolved source paths."""
        return replace(self, source_paths=source_paths)

    def compose(
        self,
        field_spec: PosixLocationSpec | None,
        field_name: str,
    ) -> DerivedLocationSpec:
        """Compose derived source paths with a nested field path selection."""
        field_paths = field_spec.paths if field_spec is not None else (field_name,)
        if not field_paths:
            field_paths = (field_name,)

        composed_patterns = tuple(
            os.path.join(os.path.expanduser(parent_path), field_path)
            for parent_path in (self.source_paths or ("",))
            for field_path in field_paths
        )
        expanded_paths = _dedupe_preserving_order(
            expanded
            for pattern in composed_patterns
            for expanded in _expand_pattern(pattern)
        )
        new_paths = expanded_paths or composed_patterns
        return replace(
            self,
            source_paths=new_paths,
            output_path=_derived_cache_path(new_paths, self.query),
        )

    def to_struct(self) -> Struct:
        """Serialize the typed derived spec back into the runtime Struct shape."""
        return Struct(
            kind="location",
            type="derived",
            name=self.name,
            abstract=False,
            _root_kind="derived",
            attributes={
                "source_ref": self.source_ref,
                "source_paths": list(self.source_paths),
                "sql_fragment": self.query.sql,
                "dialect": self.query.dialect,
                "output_path": str(self.output_path),
            },
        )


def derived_location_spec_from_value(value_struct: object) -> DerivedLocationSpec | None:
    """Resolve a derived spec from a value struct, filling source-path fallbacks."""
    location = getattr(value_struct, "location", None)
    spec = DerivedLocationSpec.from_location(location)
    if spec is None:
        return None
    if spec.source_paths:
        return spec

    source_struct = _source_value_struct(value_struct)
    source_location = getattr(source_struct, "location", None) if source_struct else None
    source_spec = PosixLocationSpec.from_location(source_location)
    if source_spec is not None:
        return spec.with_source_paths(source_spec.paths)
    if isinstance(source_struct, str) and spec.source_ref:
        return spec.with_source_paths((spec.source_ref,))
    return spec
