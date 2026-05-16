# mlody / common-python — Code Review

Scope: all Python under `common/python/` and `mlody/`, excluding `mlody/stage/`
(TypeScript/React). The review focuses on _accidental_ complexity: places where
the code knows more than it should, repeats itself, or makes adding a new case
require modifying many files instead of one.

## Executive Summary

The mlody codebase is well-modularised at the package level, but it has
accumulated several pockets of ad-hoc dispatch and boilerplate that violate the
open/closed principle and make adding new kinds of values, segments, or
strategies a multi-file affair. The biggest themes are:

1. **Registry-by-hand.** Thirteen near-identical `RegisteredX` dataclasses live
   in 13 files in `mlody/common/`, plus two parallel manual dispatch dicts in
   `_registered_struct.py`. Adding a new kind requires editing four places.
2. **Type-tag dispatch instead of polymorphism.** `_match_score`
   (`multimethod.py`), `_wrap_struct` and `_engine_*_step` (`label_value.py`),
   `_make_direct_place` (`setf.py`), and the five traversal helpers in
   `traversal_runtime.py` all encode the same shape: _check the kind, then call
   the appropriate handler_. The good news is that `mlody/core/anchor.py`
   already shows the right pattern (polymorphic `BaseAnchor`); the rest of the
   codebase has not caught up.
3. **Parallel error hierarchies and duplicated helpers.**
   `image_builder/errors.py` and `resolver/errors.py` are independent
   reinventions of the same idea. The Sonora speak/chatterbox runtimes carry
   byte-for-byte duplicates of the playback machinery.
4. **God modules.** `mlody/resolver/label_value.py` is over 3 000 lines and
   contains 11 MlodyValue subclasses, four traversal engines, two console
   rendering paths, and a stack of module-level caches. It is the single biggest
   barrier to extension.
5. **Kwarg explosions.** `resolver.py`'s baseline-workspace API threads the same
   nine kwargs through four functions; the lack of a `WorkspaceRequest` value
   object means every new option ripples through every signature.

The recommended direction is to lean harder on the patterns that already exist
in the codebase (`BaseAnchor`, `RichDomNode`, `TraversalStrategy`) and extend
them to the parts that haven't yet been polymorphised. None of the proposed
changes require new external dependencies.

## Architecture Overview

```
.mlody file (Starlark)
  -> starlarkish.Evaluator  -> Struct values
       (common/python/starlarkish/...)
  -> mlody.core.workspace.Workspace
       Phase 1: roots.mlody  -> RegisteredRoot
       Phase 2: glob .mlody  -> register* callbacks
  -> mlody.common.registry  -> RegisteredX dataclasses
       (keyed by (kind, stem, name))
  -> mlody.resolver  -> label parsing  -> traversal
       MlodyValue tree (label_value.py)
  -> common.python.console  -> RichDomNode
       (console.py rendering)
  -> mlody.cli / mlody.lsp consumers
```

The pipeline is mostly linear and clean at the package boundaries. The problems
are intra-package: duplicated dispatch, parallel hierarchies, and
hand-maintained registry tables.

## Findings

Each finding is severity-tagged:

- **H** (high) — actively impedes extension; touches many files.
- **M** (medium) — clear duplication or layering issue; refactor is mechanical
  but worthwhile.
- **L** (low) — local readability or polish.

### F1 (H) — 13 near-identical `RegisteredX` wrappers

Files:
`mlody/common/{value,action,task,user,type,config,build_ref, executor,freshness,location,implementation,representation,root}.py`

Every wrapper follows the same shape:

```python
@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredX(RegisteredStructBase):
    _KIND: ClassVar[str] = "x"
    # field declarations...

    def __init__(self, value: Struct) -> None:
        populate_from_struct(self, value)
```

The only meaningful variation is the field list and (for `action`/`task`)
post-processing of nested collections via `coerce_named_struct_collection`.
Currently:

- `populate_from_struct` already drives field assignment from dataclass
  metadata.
- Each wrapper adds nothing but a `_KIND` and an `__init__` that delegates.

**Proposed fix.** Make `RegisteredStructBase.__init_subclass__` register the
subclass against `_KIND` automatically, then have the base provide a generic
`__init__`. Wrappers shrink to:

```python
class RegisteredAction(RegisteredStructBase, kind="action"):
    impl: RegisteredImplementation
    inputs: Mapping[str, Any]
    # post-init hook for nested coercion only when needed
    def _after_populate(self) -> None: ...
```

After the refactor:

- The two dispatch dicts in `_registered_struct.py` (F2) go away — the metaclass
  already knows the kind-to-class map.
- Adding a new kind is "create one file"; no edits anywhere else.

### F2 (H) — Two parallel manual dispatch dicts

File: `mlody/common/_registered_struct.py:233-280`

`_wrapper_for_kind` and `_method_wrapper_for_kind` are hand-maintained maps from
`kind` string to wrapper class. They are co-located, list overlapping sets of
kinds, and any new `RegisteredX` requires editing both.

**Proposed fix.** Replace with a single class-level registry populated by
`__init_subclass__` (see F1). The two dicts collapse to one
`RegisteredStructBase._registry: dict[str, type[RegisteredStructBase]]`.

### F3 (H) — Five-way adapter dispatch in `traversal_runtime.py`

File: `mlody/core/traversal_runtime.py:288-392`

`step_named_child`, `step_segment`, `iter_children`, `replace_child`, and
`has_named_child` each contain the same if-chain:

```python
if isinstance(value, VirtualValue):
    return _VirtualValueAdapter(value).<op>(...)
if isinstance(value, Struct):
    return _StructAdapter(value).<op>(...)
if isinstance(value, Mapping):
    return _DictAdapter(value).<op>(...)
if isinstance(value, Sequence):
    return _SequenceAdapter(value).<op>(...)
return _ObjectAdapter(value).<op>(...)
```

Five operations × five adapters = 25 dispatch sites maintained by hand.

**Proposed fix.** Introduce a `TraversalAdapter` protocol and a single
`_adapter_for(value) -> TraversalAdapter` resolver (or use
`functools.singledispatch`). All five helpers then become one-liners:

```python
def step_named_child(value, name):
    return _adapter_for(value).step_named_child(name)
```

Adding a new container type now means "write one adapter class and register it"
rather than touching five functions.

### F4 (H) — `_match_score` if-elif chain in `multimethod.py`

File: `mlody/core/multimethod.py:31-140`

`_match_score` is a 100-line chain dispatching on `struct.kind` (`mm_any`,
`mm_scalar_pattern`, `mm_repr_pattern`, `mm_posix_pattern`, `mm_vector_pattern`,
`mm_value_pattern`, `mm_source_range_pattern`, exact string). Each branch knows
how to extract fields from the argument and compute a score.

**Proposed fix.** Introduce a `Pattern` protocol:

```python
class Pattern(Protocol):
    def score(self, arg: Any) -> int | None: ...

_PATTERN_REGISTRY: dict[str, type[Pattern]] = {}

def register_pattern(kind: str): ...  # decorator
```

`_match_score` becomes:

```python
def _match_score(pattern_struct, arg):
    pattern_cls = _PATTERN_REGISTRY.get(pattern_struct.kind)
    if pattern_cls is None:
        return 0 if pattern_struct == arg else None
    return pattern_cls.from_struct(pattern_struct).score(arg)
```

Adding a new pattern type now lives in one file alongside the pattern's data
shape.

### F5 (H) — `label_value.py` is a 3 200-line god module

File: `mlody/resolver/label_value.py`

The file contains:

- 11 `MlodyValue` subclasses with hand-rolled `to_console_representation` (lines
  165-374).
- `_wrap_struct(kind, struct)` if-chain for task/action/user/value (lines
  474-490).
- Four parallel `_engine_*_step` functions (index/key/slice/wildcard/
  recursive_descent, lines 1327-1676).
- Repeated
  `isinstance(current, (MlodyValueValue, MlodyTaskValue, MlodyActionValue, MlodyUserValue))`
  tuple checks at 5+ sites — a semantic "is a registry-backed entity?" predicate
  inlined everywhere.
- Module-level lazy caches `_ARROW_TO_MLODY_TYPE_NAME`,
  `_MLODY_PRIMITIVE_TYPE_STRUCTS`.

**Proposed fix.** Break up by concern:

| New module                                | Contents                                                     |
| ----------------------------------------- | ------------------------------------------------------------ |
| `resolver/values/base.py`                 | `MlodyValue` protocol/base                                   |
| `resolver/values/{task,action,user,…}.py` | One file per concrete value type                             |
| `resolver/render.py`                      | `dom_for(value: MlodyValue) -> RichDomNode` (singledispatch) |
| `resolver/engine/step_index.py` etc.      | One step engine per file                                     |
| `resolver/engine/dispatch.py`             | `step(segment, value)` selecting engine                      |

Replace the recurring tuple isinstance with an explicit predicate:

```python
def is_registry_backed(value: MlodyValue) -> bool:
    return getattr(value, "_registry_backed", False)
```

The wrap_struct chain becomes a registry keyed by kind, populated by the value
modules themselves.

### F6 (H) — `to_console_representation` duplicated across MlodyValue

File: `mlody/resolver/label_value.py:165-374`

`MlodyTaskValue.to_console_representation` and
`MlodyActionValue.to_console_representation` are byte-for-byte identical except
for the title prefix. Several other subclasses share large blocks verbatim.

**Proposed fix.** Extract a single `entity_panel(title, value)` helper that
takes a title and a value-like Mapping; have each subclass call it. Better,
register a renderer per type:

```python
@dom_for.register(MlodyTaskValue)
def _(v): return entity_panel("Task", v)

@dom_for.register(MlodyActionValue)
def _(v): return entity_panel("Action", v)
```

This also subsumes F5's "registry-backed predicate" — these are exactly the
entities sharing the same renderer.

### F7 (M) — Workspace builder kwargs explosion

File: `mlody/resolver/resolver.py:114-266`

`make_baseline_workspace_cache_key`, `get_or_build_baseline_workspace`,
`reload_baseline_workspace`, and `_load_baseline_workspace` all take the same
9-10 kwargs (`monorepo_root`, `workspace_root`, `roots_file`, `full_workspace`,
`print_fn`, `console`, `extra_roots`, `lazy_roots`, `resolved_sha`, `verbose`).
Adding a new option requires updating four signatures and their call-sites.

**Proposed fix.** Introduce a `WorkspaceRequest` frozen dataclass:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    monorepo_root: Path
    workspace_root: Path
    roots_file: Path
    full_workspace: bool
    extra_roots: tuple[Path, ...] = ()
    lazy_roots: bool = False
    resolved_sha: str | None = None
    verbose: bool = False

    def cache_key(self) -> tuple: ...
```

`print_fn` / `console` are output sinks; group them into a separate `Reporter`
value so they don't pollute the request itself.

The four functions then take `(request: WorkspaceRequest, reporter: Reporter)`
and are dramatically simpler.

### F8 (M) — Sonora speak/chatterbox runtime duplication

Files:

- `mlody/teams/sonora/speak/runtime.py:152-179`
- `mlody/teams/sonora/chatterbox/runtime.py:169-186`

Both files contain identical `_resolve_playback_program` and
`_build_playback_command`, plus an identical-shape `play_audio` method (the only
difference is the tempfile prefix). They also independently define a
`SpeakConfig` / `ChatterboxConfig` dataclass with the same `output_file` /
`sink` fields.

**Proposed fix.** Hoist a small shared helper to
`common/python/audio/playback.py` (new):

```python
@dataclass(frozen=True, slots=True)
class PlaybackTarget:
    output_file: Path | None
    sink: str | None

class PlaybackSession:
    """Owns paplay/aplay selection and tmpfile lifecycle."""
    def __init__(self, target: PlaybackTarget, *, prefix: str): ...
    def play(self, write_wav: Callable[[Path], None]) -> None: ...
```

Each runtime then just provides its own `write_wav` callback. Adding a third TTS
backend requires no new playback code.

### F9 (M) — Two parallel error hierarchies

Files:

- `mlody/common/image_builder/errors.py`
- `mlody/resolver/errors.py`

Both define a base exception with `exit_code` / `message` / `context` fields and
a small tree of subclasses. They were written independently and use slightly
different conventions (free-form `context` vs structured fields).

**Proposed fix.** Promote a `common/python/errors/` package containing a
`StructuredError` base:

```python
class StructuredError(Exception):
    exit_code: ClassVar[int] = 1
    def __init__(self, message: str, **context: Any): ...
    def render(self) -> RichDomNode: ...
```

Both hierarchies inherit from it. The CLI then has one rendering path for
structured errors instead of two.

### F10 (M) — Parquet handler dispatch duplicated

File: `mlody/core/parquet/deserializer.py:138-179, 354-380`

`ParquetDeserializer._convert_value` and the standalone helper
`read_file_as_rows` both consult the same handler-registry lookup, struct check,
and opaque-type sentinel logic. Drift between the two has already started.

**Proposed fix.** Extract `convert_arrow_value(value, *, types) -> MlodyValue`
as the single entry point; both consumers call it. The method on the
deserializer becomes a thin wrapper that supplies its own `types`.

### F11 (M) — `_make_direct_place` strategy-selection chain

File: `mlody/core/setf.py:174-203`

`_make_direct_place` is an if-elif chain mapping `(segment_type, owner_type)` to
a setter strategy. Adding a new segment kind or owner kind requires editing this
function.

**Proposed fix.** Replace with a dispatch table populated by the setter
strategies themselves:

```python
class FieldSetter(Protocol):
    def matches(self, seg: Segment, owner: Any) -> bool: ...

_STRATEGIES: list[type[FieldSetter]] = []
def register_strategy(cls): _STRATEGIES.append(cls); return cls

def _make_direct_place(seg, owner):
    for cls in _STRATEGIES:
        if cls.matches(seg, owner): return cls(seg, owner)
    raise _unsupported_place(seg, owner)
```

This pairs naturally with F12 — once strategies are self-registering, the
parallel preflight/commit shape can be tightened.

### F12 (M) — Setter strategies have parallel preflight/commit shape

File: `mlody/core/setf_strategies.py`

Six strategy classes share an identical skeleton: `preflight` does
type-validation + `_terminal_segment`, `commit` mutates. The boilerplate at the
top of each `preflight` is copy-paste with minor variations.

**Proposed fix.** Introduce a `FieldSetter` ABC with:

```python
class FieldSetter(ABC):
    def preflight(self):
        self._validate_segment()
        self._validate_owner()
        self._validate_value()
    def commit(self): ...
    # hooks
    def _validate_segment(self): ...
    def _validate_owner(self): ...
    def _validate_value(self): ...
```

Concrete strategies override only the hooks they need. The repeated "check
segment is terminal" pre-amble lives in the base.

### F13 (M) — Parallel `_engine_*_step` functions

File: `mlody/resolver/label_value.py:1327-1676`

`_engine_index_step`, `_engine_key_step`, `_engine_slice_step`,
`_engine_wildcard_step`, `_engine_recursive_descent_step` each implement the
same outer loop:

1. inspect current value
2. produce next-state list
3. dispatch on container kind

The container-kind dispatch overlaps with F3's adapter dispatch.

**Proposed fix.** Define a `StepEngine` protocol:

```python
class StepEngine(Protocol):
    def apply(self, value: MlodyValue, segment: Segment) -> Sequence[MlodyValue]: ...
```

Keyed by segment kind in a single dict. Inside each engine, reuse the adapter
from F3 instead of re-implementing per-container logic.

### F14 (M) — RegistryView label-matching duplication

File: `mlody/core/registry_view.py:209-420`

`match_registry_entity_label` and `expand_wildcard_label` share roughly 80 lines
of stem / root_prefix / path_suffix construction logic. The two functions return
different shapes but compute the same intermediate state.

**Proposed fix.** Extract a `_label_components(label) -> LabelComponents` helper
returning a dataclass with `root_prefix`, `stems`, `name_predicate`. Both
callers consume the same components.

### F15 (L) — `mlody-lsp --version` hangs

File: `mlody/lsp/__main__.py` (per `mlody/lsp/CLAUDE.md:144-146`)

The server immediately starts listening on stdio regardless of CLI flags.
Documented as a known wart; trivially fixed with a tiny `click` wrapper that
handles `--version`/`--help` before entering the stdio loop.

**Proposed fix.** Wrap the entry point with `click` and short-circuit on
`--version`. Costs ~10 lines, removes a developer foot-gun.

### F16 (L) — Module-level lazy caches in `label_value.py`

File: `mlody/resolver/label_value.py` (`_ARROW_TO_MLODY_TYPE_NAME`,
`_MLODY_PRIMITIVE_TYPE_STRUCTS`)

Module-globals initialised on first access make the file's load order matter and
complicate testing.

**Proposed fix.** Move into a small `TypeCatalog` class with explicit `get_*`
methods, instantiated once at workspace setup. Tests can then inject a fresh
catalog rather than mutating module state.

## Restructuring Plan

### Phase 1 — Quick Wins (low risk, mechanical)

| #   | Change                                                          | Files touched                  |
| --- | --------------------------------------------------------------- | ------------------------------ |
| 1   | Extract `entity_panel` helper to deduplicate console rendering  | `resolver/label_value.py`      |
| 2   | Extract `_label_components` helper (F14)                        | `core/registry_view.py`        |
| 3   | Add `PlaybackSession` to `common/python/audio/`; consume in TTS | sonora/{speak,chatterbox}/     |
| 4   | Unify parquet `convert_arrow_value` entry point (F10)           | `core/parquet/deserializer.py` |
| 5   | Add `--version` short-circuit to lsp entry (F15)                | `lsp/__main__.py`              |
| 6   | Replace tuple-isinstance with `is_registry_backed` predicate    | `resolver/label_value.py`      |
| 7   | Introduce `WorkspaceRequest` / `Reporter` value objects (F7)    | `resolver/resolver.py`         |

All of these are scoped to one or two modules and add no new abstractions to the
system as a whole.

### Phase 2 — Structural (medium risk, removes duplication classes)

| #   | Change                                                                                 | Files touched                                                                                     |
| --- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 8   | `RegisteredStructBase.__init_subclass__` registry; remove `_wrapper_for_kind` (F1, F2) | `common/_registered_struct.py`, all `mlody/common/{kind}.py`                                      |
| 9   | `TraversalAdapter` protocol + `_adapter_for` (F3)                                      | `core/traversal_runtime.py`                                                                       |
| 10  | `Pattern` protocol + registry for `_match_score` (F4)                                  | `core/multimethod.py`                                                                             |
| 11  | `FieldSetter` ABC + self-registration (F11, F12)                                       | `core/setf.py`, `core/setf_strategies.py`                                                         |
| 12  | Unified `StructuredError` base (F9)                                                    | `common/python/errors/` (new), `mlody/common/image_builder/errors.py`, `mlody/resolver/errors.py` |

Phase 2 reduces 5-way and 3-way ad-hoc dispatch to one-line lookups and makes
every affected module open to extension.

### Phase 3 — Deep Architecture (high impact, deliberate)

| #   | Change                                                                                                                                        | Files touched                            |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 13  | Split `label_value.py` into `resolver/values/*`, `resolver/render.py`, `resolver/engine/*` (F5)                                               | new package tree under `resolver/`       |
| 14  | `dom_for: singledispatch[MlodyValue] -> RichDomNode` for console rendering (F6)                                                               | `resolver/render.py`, callers in cli/lsp |
| 15  | `StepEngine` protocol + segment-kind dispatch; engines re-use adapters from Phase 2 (F13)                                                     | `resolver/engine/*`                      |
| 16  | Promote `RegisteredStructBase` registry + `Pattern` registry + `StepEngine` registry into a single coherent extension API (`mlody.extension`) | `mlody/extension/` (new)                 |

Phase 3 produces a codebase where adding "a new kind of value" means writing one
file: a dataclass that registers itself, a renderer that registers itself, and
(if applicable) a step engine that registers itself. No central if-chain needs
editing.

## File-Level Change Summary

| File                                                                                                                            | Phase | Action                                                                              |
| ------------------------------------------------------------------------------------------------------------------------------- | ----- | ----------------------------------------------------------------------------------- |
| `mlody/common/_registered_struct.py`                                                                                            | 2     | Replace two manual dispatch dicts with `__init_subclass__` registry                 |
| `mlody/common/{value,action,task,user,type,config,build_ref,executor,freshness,location,implementation,representation,root}.py` | 2     | Drop boilerplate `__init__`; declare `kind=` in class header                        |
| `mlody/core/traversal_runtime.py`                                                                                               | 2     | Collapse five if-chains into single `_adapter_for(value)` dispatch                  |
| `mlody/core/multimethod.py`                                                                                                     | 2     | Replace `_match_score` with `Pattern` protocol registry                             |
| `mlody/core/setf.py`                                                                                                            | 2     | Strategy list with `matches(seg, owner)` selector                                   |
| `mlody/core/setf_strategies.py`                                                                                                 | 2     | `FieldSetter` ABC with hook methods; remove boilerplate                             |
| `mlody/core/registry_view.py`                                                                                                   | 1     | Extract `_label_components` helper                                                  |
| `mlody/core/parquet/deserializer.py`                                                                                            | 1     | Single `convert_arrow_value` entry point                                            |
| `mlody/resolver/resolver.py`                                                                                                    | 1     | `WorkspaceRequest` / `Reporter` value objects                                       |
| `mlody/resolver/label_value.py`                                                                                                 | 3     | Split into `resolver/values/*`, `resolver/render.py`, `resolver/engine/*`           |
| `mlody/resolver/errors.py`                                                                                                      | 2     | Inherit from `common.python.errors.StructuredError`                                 |
| `mlody/common/image_builder/errors.py`                                                                                          | 2     | Inherit from `common.python.errors.StructuredError`                                 |
| `mlody/teams/sonora/speak/runtime.py`                                                                                           | 1     | Use shared `PlaybackSession`                                                        |
| `mlody/teams/sonora/chatterbox/runtime.py`                                                                                      | 1     | Use shared `PlaybackSession`                                                        |
| `mlody/lsp/__main__.py`                                                                                                         | 1     | Add `--version` short-circuit                                                       |
| `common/python/audio/playback.py`                                                                                               | 1     | New module: `PlaybackTarget`, `PlaybackSession`                                     |
| `common/python/errors/__init__.py`                                                                                              | 2     | New module: `StructuredError`                                                       |
| `mlody/extension/__init__.py`                                                                                                   | 3     | New module: unified `register_kind`, `register_pattern`, `register_step_engine` API |

## Target Outcome

After Phase 2, the answer to "how do I add a new struct kind?" is:

> Write one dataclass in `mlody/common/` with `kind="…"` in the class header.
> Nothing else changes.

After Phase 3, the answer to "how do I add a new MlodyValue type?" is:

> Write one value class, one renderer, and (if needed) one step engine. Each one
> self-registers. No existing file in the resolver pipeline is modified.

That is the bar — not "check 12 cases and hope we didn't miss one."
