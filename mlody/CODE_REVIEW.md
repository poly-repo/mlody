# Code Review

Review scope: Python in `cli`, `common`, and `core`, with a bias toward simplicity, composability, and long-term maintainability.

## Main Findings

- High: tabular/parquet/derived behavior is spread across ad hoc struct inspection in `cli/show.py:437`, `core/location_composition.py:70`, `core/derived.py:97`, `core/sql/sql_query.py:78`, and `core/parquet/deserializer.py:78`. The same concepts are repeatedly inferred from raw `type` strings and `attributes` dicts. Adding a new storage or query kind would require editing branching logic in several places.

- High: `Workspace` is carrying too many responsibilities in one class. `core/workspace.py:265`, `core/workspace.py:470`, and `core/workspace.py:556` mix anchor resolution, registry lookup, lazy module loading, root management, port-shape conversion, label traversal, and evaluator adaptation. The class also reaches into evaluator internals such as `_roots_by_name`, `_module_globals`, `loaded_files`, and `_types_by_name`, which makes future extension brittle.

- High: selector traversal is implemented as an `isinstance` matrix rather than a composable abstraction. The branching is spread across `core/setf.py:57`, `core/setf.py:176`, `core/setf.py:225`, `core/setf.py:331`, and `core/setf_strategies.py:18`. Similar "list-by-name else getattr" traversal is duplicated in `core/workspace.py:156` and `core/virtual_value.py:42`. New segment or container types will require edits in multiple places.

- Medium: `Struct` is too thin, so callers keep re-implementing copy, update, and traversal semantics and sometimes rely on internals. See `common/struct.py:5`, `core/lineage.py:31`, `core/workspace.py:362`, and `core/setf_strategies.py:31`. This is a major reason behavior is split between raw dicts, `Struct`, and "objects with `attributes`".

- Medium: the DAG CLI presentation layer is duplicated almost verbatim between `cli/dag_cmd.py:24` and `cli/show.py:611`. That creates guaranteed drift and means every change to port formatting, task rendering, or subgraph selection has to be made in two places.

- Medium: `common/huggingface/model-download.py:161` is a monolithic script that mixes transport, resume state, repo introspection, CLI parsing, repo-type flags, and progress output. In contrast, the image builder code is already split into clearer phases and protocols in `common/image_builder/pipeline.py:18` and `common/image_builder/auth.py:16`.

## Refactoring Plan

- Introduce a small `TabularSource` abstraction for "something queryable, previewable, and materializable", with concrete implementations such as `ParquetSource` and `DerivedSource`.
  - Move path normalization, cache key generation, preview, count, and materialization behind that interface.
  - Goal: adding a new storage kind should mean adding a new implementation, not editing `show.py`, `derived.py`, `location_composition.py`, and `sql_query.py`.

- Split `Workspace` into smaller services such as `WorkspaceLoader`, `AnchorResolver`, and `RegistryView`.
  - Replace stringly `writeback_kind` branching with explicit anchor objects.
  - Keep `Workspace` as a thin facade if needed.

- Centralize traversal with one segment-dispatch layer.
  - Either use `functools.singledispatch` on segment type plus small owner protocols, or use a small registry of strategy objects keyed by segment class.
  - Make `setf`, `workspace`, and `virtual_value` all call that shared layer.

- Give `Struct` either a richer API or a clearer protocol boundary.
  - Options: add methods like `replace`, `child`, `children`, and `with_fields`, or define a `StructLike` protocol and stop making every caller rebuild structs manually.
  - Goal: stop leaking `_fields`-level concerns into business logic.

- Extract shared DAG rendering and selection helpers into a dedicated CLI module such as `cli/dag_render.py`.
  - `show` and `dag` should share one rendering path, not duplicate helpers.

- Break `common/huggingface/model-download.py` into command handlers plus a small client object and a `RepoType` enum.
  - Keep current behavior, but make "new repo kind", "new resume backend", or "new output mode" additive instead of invasive.

## Key Rewrite Directions

### 1. Tabular data abstraction

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa


@dataclass(frozen=True)
class QuerySpec:
    sql: str
    dialect: str = "duckdb"


class TabularSource(Protocol):
    def preview(self, limit: int) -> pa.Table: ...
    def count(self) -> int: ...
    def materialize(self) -> Path: ...


@dataclass(frozen=True)
class ParquetSource:
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class DerivedSource:
    upstream: TabularSource
    query: QuerySpec
    cache: "CacheStore"
```

Why this helps:

- `show.py` stops branching on `location.type == "derived"` vs "plain parquet".
- cache behavior stops leaking across modules.
- query-related behavior lives with queryable data.

### 2. Explicit label anchors

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Anchor(Protocol):
    def resolve(self, workspace: "Workspace") -> object: ...
    def writeback(self, workspace: "Workspace", value: object) -> None: ...


@dataclass(frozen=True)
class RegistryEntityAnchor:
    key: tuple[object, object, object]
    field_parts: tuple[str, ...]


@dataclass(frozen=True)
class ModuleGlobalAnchor:
    file_path: Path
    symbol: str
    field_parts: tuple[str, ...]
```

Why this helps:

- replaces `writeback_kind` switches with polymorphism.
- keeps anchor-specific behavior near the anchor type.
- makes new anchor types open for extension instead of forcing edits in `Workspace`.

### 3. Shared traversal dispatch

```python
from functools import singledispatch


@singledispatch
def step(container: object, segment: object) -> object:
    raise TypeError(f"Unsupported traversal: {type(container).__name__}, {type(segment).__name__}")


@step.register
def _(container: Struct, segment: FieldSegment) -> object:
    return getattr(container, segment.name)


@step.register
def _(container: list, segment: IndexSegment) -> object:
    return container[segment.index]


@step.register
def _(container: dict, segment: KeySegment) -> object:
    return container[segment.key]
```

Why this helps:

- one place defines traversal behavior.
- `setf`, `workspace`, and `virtual_value` can reuse the same rules.
- adding a new segment or container type becomes additive.

## Optional Alternative Designs

### Conservative path

- Keep the current free-function style.
- Add a few protocols and registries.
- Extract duplication first.

Best when you want lower migration risk and smaller PRs.

### Stronger object model

- Make storage, traversal, and anchors first-class objects.
- Use polymorphism more aggressively.

Best when these domains are expected to keep growing.

## Recommended Order

1. Extract shared DAG rendering helpers.
2. Introduce a shared traversal layer.
3. Add `Struct` helpers or a `StructLike` protocol.
4. Extract tabular/parquet/derived abstractions.
5. Split `Workspace` once the lower-level seams are in place.

## Summary

The dominant source of accidental complexity is not individual large functions. It is the same concepts being re-derived from loosely structured objects in many places. The biggest win is to stop asking "what kind of thing is this?" all over the codebase and instead make the thing know what to do.
