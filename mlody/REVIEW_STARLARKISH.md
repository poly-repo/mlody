# Starlarkish Leakage Audit

## Summary

`common/python/starlarkish/` is, by name and stated intent, a generic Starlark
evaluator. In reality, the `evaluator/` subtree is a thinly-disguised mlody
runtime. The `core/struct.py` module is clean; `evaluator/registry.py` is
hardcoded to mlody's twelve-plus entity kinds; and `evaluator/evaluator.py` is
saturated with mlody concepts at every layer — type identifiers (`mlody-root`,
`mlody-value`, `mlody-source-range`), domain-specific resolution logic
(`resolve()` walks `actions`/`tasks` and recurses through
`inputs`/`outputs`/`config`), magic field names tied to mlody's data model
(`_entity_type`, `_lineage`, `raw`, `materializer`, `_allowed_attrs`,
`virtual`), a sandbox builtin flag named `__MLODY__`, and even **upward
imports** from `mlody.common` and `mlody.core.multimethod` (lines 67, 159, 168,
1321). Test files inside `common/python/starlarkish/evaluator/` use `.mlody`
extensions and assert against mlody entity kinds.

Beyond the entity kind hardcoding, the evaluator also embeds a grab bag of
domain-specific external integrations: astropy unit parsing
(`_parse_astropy_unit`, `_parse_quantity_string`, `_format_quantity_string`),
GitHub-aware HTTP metadata fetching (`_parse_github_content_target`,
`_github_http_info`), git-rev-parse-based commit SHA expansion
(`_expand_commit_sha`), and UUID v7 generation. These are exposed under the
`python.*` sandbox namespace and exist for mlody scripts to call — none of them
have anything to do with Starlark evaluation. The Bazel target `evaluator_lib`
accordingly carries `@pip//astropy` and `@pip//uuid_utils` deps, which a
"generic" Starlark library has no business demanding.

The severity is high. The leakage is not a few stray identifiers — it's the core
data model of the evaluator. Any second consumer of `starlarkish` would either
inherit mlody's twelve registration kinds and "mlody-\*" type names, or would
need to bypass them. The architecture should be flipped: `starlarkish` should
know only how to evaluate Starlark and dispatch host calls; mlody's entity
model, resolution semantics, materializers, virtual values, source ranges,
registered-struct decoration, and domain helpers should all live under
`mlody/evaluator/` (or similar). What remains in `common/python/starlarkish/`
should be: `Struct`, `struct()`, `SAFE_BUILTINS`, the `load()` machinery
parameterised on file extension, sandbox install/exec, a generic
`builtins.register(kind, thing)` that delegates to a host-provided callback, and
`InMemoryFS` parameterised on extension.

## Methodology

I listed both `common/python/` subdirectories and read every file in
`starlarkish/` (`__init__.py`, `core/struct.py`, `evaluator/registry.py`,
`evaluator/evaluator.py`, `evaluator/testing.py`, and `STARLARK_DEVIATIONS.md`).
I greped `common/python` for the suspect tells (`mlody`, `.mlody`, `stage`,
`pipeline`, `dataset`, `training`, `feature`, `label`, `registry`) and for the
hardcoded entity kind strings (`'root'`, `'value'`, `'task'`, `'action'`,
`'user'`, `'representation'`, `'location'`, `'freshness'`, `'build_ref'`,
`'implementation'`, `'executor'`, `'generic'`, `'config'`). I then verified each
candidate by checking actual callers via grep on
`from common.python.starlarkish` across the repo; every caller of the non-`core`
APIs lives in `mlody/`. Finally, I cross-checked the suspect external-helper
functions (`_parse_astropy_unit`, `_github_http_info`, `_expand_commit_sha`,
`_uuid7_string`) against the project's `mlody/CLAUDE.md` description of the DSL
surface to confirm they exist for mlody scripts.

## Confirmed Leaks

### Reverse imports from `mlody` into starlarkish

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:67-72`
  (deferred import of `mlody.common.struct`), `:159`
  (`mlody.common._registered_struct`), `:168` (same), and `:1321`
  (`from mlody.core.multimethod import dispatch`).
- **Why it's a leak**: This is the most flagrant violation possible — a library
  in `common/` directly importing from the application it is supposed to be
  agnostic of. The `try/except ModuleNotFoundError` pattern at line 66 is a
  confession: the code expects mlody to be present, but ships a degraded
  fallback so the package can technically import without it. The dispatch call
  at 1321 has no fallback; `_dispatch_method_impl` is dead without mlody.
- **Where it should live**: All of this code (`_register_method_impl`,
  `_get_methods_impl`, `_dispatch_method_impl`, the `wrap_registered_struct` /
  `wrap_method_result` calls in `decorate_registered_value`) belongs in a new
  `mlody/evaluator/` package. The generic evaluator should expose a
  `host_callbacks` extension point (e.g. `register_method`, `lookup_method`,
  `dispatch_method`, `wrap_value`) that the mlody side wires up.
- **Migration sketch**: Move the four code paths to mlody; turn the
  `Builtins.register_method` / `get_methods` / `dispatch_method` / decoration
  hooks into a single `HostExtensions` injection point on `Evaluator.__init__`.
  Update mlody's `WorkspaceLoader` to construct the evaluator with its own
  extensions.

### `RegistryState` hardcoded to mlody entity kinds

- **Location**: `common/python/starlarkish/evaluator/registry.py:9-24, 72-185`.
- **Why it's a leak**: `SUPPORTED_REGISTRATION_KINDS` is a closed enumeration of
  mlody's domain entities (`root`, `type`, `location`, `freshness`,
  `representation`, `value`, `action`, `task`, `user`, `implementation`,
  `build_ref`, `executor`, `generic`, `config`). `RegistryState` mirrors that as
  twelve named fields with a hand-rolled `for_kind` `match` statement. A
  genuinely generic registry would be one `dict[str, NamedRegistry]` keyed by an
  externally-supplied kind name.
- **Where it should live**: The generic shape (`NamedRegistry`, the
  by_key/by_name pattern, `aggregate_sink`) is fine — move to
  `common/python/starlarkish/evaluator/registry.py` as a small unparameterised
  primitive. The mlody-specific kind list and the typed accessors
  (`registry.roots`, `registry.tasks`, etc.) should move to
  `mlody/evaluator/registry.py` as a thin subclass or builder.
- **Migration sketch**: Replace `RegistryState` with a generic `KindedRegistry`
  that exposes `registries: dict[str, NamedRegistry]` and `for_kind(kind)`;
  instantiate the mlody-specific bundle in `mlody/evaluator/registry.py`. Update
  `Evaluator` (after splitting it, below) to take `kinds: Iterable[str]` instead
  of hardcoding them.

### `_ENTITY_DESCRIPTOR_TYPE_NAMES` + `_METHOD_ENTITY_KINDS` + `_LEGACY_REGISTRY_ATTRS`

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:99-105`
  (descriptor type names hardcoded to `"mlody-root"`, `"mlody-value"`,
  `"mlody-task"`, `"mlody-action"`, `"mlody-user"`), `:107-136`
  (`_LEGACY_REGISTRY_ATTRS` lists every mlody entity kind individually),
  `:137-153` (`_METHOD_ENTITY_KINDS` enumerates the dozen kinds that can carry
  `methods`).
- **Why it's a leak**: These are mlody's domain vocabulary embedded as string
  literals in what is supposed to be a Starlark engine. The `"mlody-*"` prefix
  is unambiguous.
- **Where it should live**: `mlody/evaluator/entity_kinds.py` (or
  `mlody/core/entity_kinds.py`). The generic evaluator should expose a pluggable
  type-descriptor lookup rather than reading
  `self.registry.types.by_name.get("mlody-source-range")` directly.
- **Migration sketch**: Lift these constants into mlody. Have the generic
  evaluator accept a `value_decorator: Callable[[str, Named], Named]` hook on
  `__init__` and call it from `_register`; move `decorate_registered_value`,
  `_decorate_source_range`, `_materialized_child_specs`,
  `_make_materialized_child_value`, and `_refresh_declared_entity_types` into
  the mlody-side decorator.

### `resolve()` and the action/task port-resolution pipeline

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:1818-1893`.
- **Why it's a leak**: This method is pure mlody domain logic: it iterates
  `registry.actions` and `registry.tasks`, resolves string labels in
  `inputs`/`outputs`/`config`/`action` fields to `value`/`action` entities, and
  re-decorates as `"action"` and `"task"` kinds. None of this concerns Starlark
  evaluation.
- **Where it should live**: `mlody/evaluator/resolve.py` (or as a method on a
  `mlody/evaluator/MlodyEvaluator` subclass / wrapper that owns the domain
  phase).
- **Migration sketch**: Move the method body. The generic evaluator should not
  expose a `resolve()` step at all — it loads files and that's the end of the
  contract. Any post-processing belongs to the caller.

### `_is_virtual_value_struct`, `_force_virtual_value_struct`, materializer plumbing

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:486-509, 1486-1523, 1556-1580`.
- **Why it's a leak**: "Virtual values" with materializers are an mlody concept
  — they describe lazily-computed value entities whose `location` has
  `type="virtual"` and a `materializer` callable. The generic Starlark surface
  doesn't have values, locations, or types.
- **Where it should live**: `mlody/evaluator/virtual_value.py`. The Struct type
  itself stays in starlarkish; the predicate and forcing helper move next to the
  consumers (`_runtime_json_data` uses them, and that whole function is
  mlody-flavored too — see below).
- **Migration sketch**: Move these helpers and the
  `_make_materialized_child_value` / `_materialized_child_specs` methods. The
  generic `_runtime_json_data` should accept a `value_forcer` callback instead
  of hard-coding `_force_virtual_value_struct`.

### `_runtime_json_data` and `_looks_like_workspace`

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:512-588`.
- **Why it's a leak**: `_looks_like_workspace` is an explicit duck-type check
  for `mlody.core.workspace.Workspace` (the comment at line 513 literally says
  so). It special-cases `_monorepo_root`, `_workspace_root`, `root_infos`, and
  `info` — fields private to mlody's `Workspace` class. `_runtime_json_data`
  also strips fields named `_evaluator`/`evaluator`, `raw`, `_entity_type` — all
  mlody-specific.
- **Where it should live**: `mlody/cli/runtime_json.py` (the function is called
  by `mlody/cli/server.py:32` as `_runtime_json_data` and reused for
  `runtime_json_blob` via `python.runtime_json_blob` in the sandbox).
- **Migration sketch**: Move the function. The CLI keeps importing it from
  `mlody.cli.runtime_json` instead of the private `_runtime_json_data`. Drop the
  entire `python.runtime_json_blob` exposure from the generic sandbox; mlody's
  host can inject it via the host-extension callback hook.

### `python.parse_astropy_unit`, `python.parse_quantity_string`, `python.format_quantity_string`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:449-478, 938-940`,
  registered into `PYTHON_SPECIFIC_BUILTINS`.
- **Why it's a leak**: Astropy is a science-domain dependency for physical
  units. There is no Starlark connection. The Bazel target carries
  `@pip//astropy` as a dependency
  (`common/python/starlarkish/evaluator/BUILD.bazel:14`), which is by itself
  proof — a "generic Starlark evaluator" should not depend on Astropy.
- **Where it should live**: `mlody/sandbox/units.py` or similar, registered by
  mlody when it builds the host-extensions struct.
- **Migration sketch**: Move the three helpers; drop the astropy dep from the
  generic `evaluator_lib`. Have mlody's host inject these via the
  `Builtins.inject` channel or via a `host_python_extras: dict[str, Any]`
  parameter on `Evaluator.__init__`.

### `python.http_info` (incl. all GitHub-aware code)

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:735-927, 937`
  (registered as `http_info`).
- **Why it's a leak**: `_HTTP_INFO_USER_AGENT = "mlody-http-info/1.0"` at line
  98 hardcodes the mlody product name in a User-Agent string. The hundreds of
  lines of GitHub Contents / Commits API parsing, HEAD/GET fallback logic,
  ETag/MD5/Last-Modified extraction — none of it is Starlark. It exists because
  mlody scripts call `python.http_info(uri)` to populate `location` metadata.
- **Where it should live**: `mlody/sandbox/http_info.py`.
- **Migration sketch**: Move all of `_coerce_http_length`,
  `_normalize_http_update_time`, `_normalize_http_digest`,
  `_extract_http_digest`, `_http_headers_info`, `_github_request`, `_load_json`,
  `_parse_github_content_target`, `_github_contents_api_url`,
  `_github_commits_api_url`, `_extract_github_update_time`, `_github_http_info`,
  `_generic_http_info`, `_http_info`, the `_GitHubContentTarget` dataclass, and
  the constant `_HTTP_INFO_USER_AGENT` into mlody. Inject via host extras.

### `python.expand_commit_sha`

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:591-631, 954`
  (and imports `git as _git` lazily inside the function).
- **Why it's a leak**: Git commit-SHA resolution is mlody-script functionality.
  The generic evaluator has no reason to invoke `git rev-parse`.
- **Where it should live**: `mlody/sandbox/git_helpers.py`.
- **Migration sketch**: Move alongside the `_FULL_SHA_RE` / `_SHORT_SHA_RE`
  constants. Inject via host extras.

### `python.uuid7`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:64 (import), 481-483, 942`.
  Bazel dep `@pip//uuid_utils` at `BUILD.bazel:15`.
- **Why it's a leak**: UUIDv7 generation is again a mlody-script convenience
  exposed under `python.uuid7`. Not Starlark.
- **Where it should live**: `mlody/sandbox/uuids.py`.
- **Migration sketch**: Move the helper; inject via host extras; drop the
  `@pip//uuid_utils` dep from the generic library.

### Hardcoded sentinel-type seeding (`integer`/`string`/`bool`/`float`)

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:1205-1213`.
- **Why it's a leak**: This pre-registers four primitive `type` entities in the
  `types` registry at evaluator construction time. The `type` kind is itself
  mlody-specific (see registry hardcoding above). These sentinels exist because
  mlody's typed-value model needs primitive types referenceable by name.
- **Where it should live**: `mlody/evaluator/bootstrap.py`, run by mlody's
  evaluator subclass / factory.
- **Migration sketch**: Move the seeding to wherever the mlody-evaluator is
  constructed. The generic `Evaluator` should not seed any kinds.

### `__MLODY__` sandbox global

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:1332`
  (`sandbox_globals["__MLODY__"] = True`).
- **Why it's a leak**: A sandbox flag named after the product. Used by `.mlody`
  scripts to detect they are running inside the mlody host.
- **Where it should live**: mlody-side injection via `_persistent_injections` or
  `Builtins.inject`. Or simply removed once the host-extension hook exists.
- **Migration sketch**: Delete this line; if scripts need a host-detection flag,
  set it from mlody's bootstrap via the inject channel.

### `Builtins.register_method` / `get_methods` / `dispatch_method`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:1163-1177, 1293-1323, 1338-1340`.
- **Why it's a leak**: The `_method_registry` machinery is mlody's multimethod /
  generic-function dispatch. The comment at lines 1174-1177 is explicit:
  "`dispatch_method` wraps `mlody.core.multimethod.dispatch` so that
  `mm.mlody`'s `dispatch_fn` can call it without a Python-style import". A
  `mm.mlody` reference is a domain fingerprint.
- **Where it should live**: `mlody/evaluator/multimethod_bridge.py`, composed
  alongside the existing `mlody.core.multimethod`.
- **Migration sketch**: Remove these three fields from `Builtins`; have mlody's
  host inject `mm` / `defmethod` via the existing `_persistent_injections`
  channel, calling its own `_method_registry`-equivalent without involving the
  generic evaluator.

### `_decorate_source_range` / `_make_source_range_struct`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:1442-1449, 1582-1598`. The
  struct emitted has `"kind": "mlody-source-range"`.
- **Why it's a leak**: Source-range metadata with a `"mlody-source-range"` kind
  is again mlody domain-specific. The generic load mechanism can pass raw
  `(filepath, start_line, end_line)` tuples to a host callback; the decision to
  wrap them as Structs with an `_entity_type` is mlody's.
- **Where it should live**: `mlody/evaluator/source_range.py`.
- **Migration sketch**: Move both methods. The generic `_register` should call a
  `value_decorator(kind, thing, source_range_or_none)` callback if set; mlody
  supplies one.

### `line_range_extractor` hook + `_file_ranges`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:1191-1194, 1227, 1775-1778, 1637-1646`.
- **Why it's a leak**: Less obvious than the others. The hook signature itself
  is generic (`Callable[[Path, str], dict[tuple[str, str], tuple[int, int]]]`),
  but its only purpose is to feed the `_decorate_source_range` machinery above.
  With the source-range decoration moved, this hook can move too — it has no
  other consumer.
- **Where it should live**: mlody side, behind the `value_decorator` callback
  proposed above.
- **Migration sketch**: When the decorator hook is added, push the line-range
  extractor into it as a closure.

### `_validate_loads_at_top` referencing `.mlody` extension (cosmetic)

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:388-418` and
  module docstring lines 27-36, 1680-1684.
- **Why it's a leak**: The validation logic is generic Starlark — fine to keep —
  but the docstrings, error messages, and STARLARK_DEVIATIONS.md
  (`common/python/starlarkish/STARLARK_DEVIATIONS.md:73, 83, 93`) all reference
  `.mlody` files specifically.
- **Where it should live**: The validator stays in starlarkish; only the
  references to the file extension need to be parameterised or genericised in
  comments and docs.
- **Migration sketch**: Optionally make the file extension configurable via
  `Evaluator(..., file_extension=".mlody")` or just talk about "script files" in
  docs. Low priority but worth fixing during the move.

### `testing.InMemoryFS` docstring assumes `.mlody`

- **Location**: `common/python/starlarkish/evaluator/testing.py:3-16`.
- **Why it's a leak**: The class itself is generic and useful (it mocks `open`
  and `Path.resolve`); only its docstring is mlody-shaped. The example uses
  `.mlody` filenames and references `evaluator.roots[...]` (an mlody-specific
  accessor).
- **Where it should live**: Stay in starlarkish; rewrite the docstring to use
  generic filenames and avoid the `evaluator.roots` example.
- **Migration sketch**: Pure docstring change.

### `evaluator_test.py` and `evaluator_generics_test.py` use mlody domain in tests

- **Location**: `common/python/starlarkish/evaluator/evaluator_test.py` (24+
  registrations of `"root"` / `"value"` / `"task"` etc.),
  `common/python/starlarkish/evaluator/evaluator_generics_test.py`
  (mlody-specific kind tests at lines 42, 56, 253-261, 275, 294-310).
- **Why it's a leak**: The tests assert mlody semantics — that `'root'`,
  `'config'`, `'user'`, etc. are valid registration kinds. Once the registry is
  genericised, these tests no longer test the generic library; they test mlody's
  instantiation of it.
- **Where it should live**: Most of these tests should move to
  `mlody/evaluator/`. A handful of truly generic tests (load resolution,
  loads-at-top validation, sandbox-builtin coverage, Struct semantics) can
  remain.
- **Migration sketch**: After the registry split, sort each test into "tests the
  generic evaluator" vs. "tests mlody's entity model" and relocate accordingly.

## Suspected Leaks (lower confidence)

### `_normalize_methods_recursively` + `_wrap_starlark_method` + the `__mlody_*` markers

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:175-362`.
- **Why it's suspected**: This is a substantial chunk of code (≈190 lines) that
  walks Struct trees, validates that "method" names don't collide with entity
  attributes, wraps callables with an `entity`/`enclosing_entity` calling
  convention, and calls `wrap_method_result` from
  `mlody.common._registered_struct`. The markers `__mlody_raw_callable__` and
  `__mlody_method_wrapper__` are explicit product names. This very strongly
  leans toward leak — there is no generic Starlark notion of "entity methods" —
  but it is conceivable that a "Starlark plus methods on structs" extension is
  reusable.
- **Where it should live**: `mlody/evaluator/methods.py` unless a second
  consumer materialises.
- **Migration sketch**: Move. The generic side would simply not know about
  entity methods.

### `Builtins.register` / `lookup` taking a `kind: str`

- **Location**:
  `common/python/starlarkish/evaluator/evaluator.py:1164-1167, 1270-1278, 1656-1663`.
- **Why it's suspected**: A generic registry-by-kind is plausibly reusable —
  it's a common pattern for sandboxed DSLs. The kind names being mlody's is what
  makes it leak today. If we replace the closed kinds list with an open
  `kinds: Iterable[str]` parameter on `Evaluator.__init__`, the
  `Builtins.register(kind, thing)` API stays generic.
- **Where it should live**: Keep `register` / `lookup` in the generic evaluator
  with externally-supplied kinds.
- **Migration sketch**: Parameterise `RegistryState` (see registry leak above);
  no change to the `Builtins.register` API itself.

### `_lookup` error message includes `Available: {sorted(registry.by_name)}`

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:1662`.
- **Why it's suspected**: Generic, but worth noting because the helpful list of
  available names is only meaningful if there's an enumerable set of kinds. Fine
  once the registry is genericised.

### `print_fn` / `extra_ctx` / `resolve_hook` / `force_hook` / `setf_hook`

- **Location**: `common/python/starlarkish/evaluator/evaluator.py:1185-1198`.
- **Why it's suspected**: `resolve`, `force`, and `setf` are mlody-specific
  language features (lazy resolution, value forcing, field-set). They are
  exposed as sandbox builtins when the hooks are non-None. They're pluggable
  hooks, so the generic evaluator is technically agnostic — but the names
  `resolve`/`force`/`setf` baked into the sandbox global names are not generic
  Starlark vocabulary.
- **Where it should live**: Either rename to
  `Evaluator(..., extra_globals: dict[str, Callable])` (fully generic) or accept
  that the three names are mlody-leakage and move them out.
- **Migration sketch**: Replace with a single `extra_globals: dict[str, Any]`
  parameter. Mlody passes `{"resolve": ..., "force": ..., "setf": ...}` from its
  side.

## Generic Code Worth Keeping

- **`common/python/starlarkish/core/struct.py`** — `Struct` and `struct()` are
  exemplary generic Starlark types; no leakage.
- **`common/python/starlarkish/__init__.py`** — clean lazy-export façade.
- **`common/python/starlarkish/evaluator/testing.py` (`InMemoryFS`)** — generic
  except for docstring; keep after a docstring rewrite.
- **`_validate_loads_at_top`, `_sandbox_type`, `SAFE_BUILTINS`,
  `_install_sandbox_runtime_bindings`, `_load` (path resolution),
  `_execute_file` (sandbox exec mechanics), `_copy_function_with_globals`,
  `_clone_runtime_visible_value`, `fork()`** — these are generic Starlark
  evaluation primitives. Their references to `.mlody` extension are cosmetic and
  easily parameterised.
- **`common/python/console/`** — entirely generic Rich DOM helpers; no leakage
  at all.
- **`NamedRegistry`** in `registry.py` (the type itself) — generic shape; only
  the closed `SUPPORTED_REGISTRATION_KINDS` list and the typed `RegistryState`
  accessors are mlody-specific.

## Migration Plan

A suggested ordered sequence. Each step is independently shippable; pause
between steps to run `bazel test //...`.

1. **Move the four reverse imports first.**
   - Move `_dispatch_method_impl`'s call to `mlody.core.multimethod` out by
     introducing a `dispatch_method` callback on `Evaluator.__init__` (mlody
     wires it up).
   - Move the `mlody.common.struct` `_is_struct_like` / `_struct_like_*`
     fallbacks out by making the generic evaluator only accept `Struct` (it's
     the only "struct-like" thing it has direct knowledge of anyway); push the
     dataclass-friendly path into mlody.
   - Move `_wrap_registered_value` / `_wrap_method_result_value` out via a
     `value_decorator` callback.
   - After this step, `common/python/starlarkish/evaluator/evaluator.py` has no
     `from mlody.*` imports.

2. **Genericise `RegistryState`.**
   - Convert `RegistryState` to `KindedRegistry` with
     `registries: dict[str, NamedRegistry]` and `kinds: tuple[str, ...]` passed
     in. Remove `SUPPORTED_REGISTRATION_KINDS` from
     `common/python/starlarkish/evaluator/registry.py`.
   - Create `mlody/evaluator/registry.py` with a `make_mlody_registry()` factory
     that returns a `KindedRegistry` pre-populated with the twelve kinds, plus
     typed accessor properties (`.roots`, `.tasks`, etc.) for ergonomic
     mlody-side use.
   - Remove `_LEGACY_REGISTRY_ATTRS` from
     `common/python/starlarkish/evaluator/evaluator.py`; mlody's typed accessors
     replace it.

3. **Pull out the mlody-specific decorators.**
   - Move `_ENTITY_DESCRIPTOR_TYPE_NAMES`, `_METHOD_ENTITY_KINDS`,
     `_METHOD_RESERVED_ATTRIBUTE_NAMES`, `_normalize_method_mapping`,
     `_normalize_methods_recursively`, `_wrap_starlark_method`,
     `_validate_method_names`, `_method_collision_names`, `_method_items`,
     `_declared_child_specs`, `_decorate_source_range`,
     `_materialized_child_specs`, `_make_materialized_child_value`,
     `decorate_registered_value`, `_refresh_declared_entity_types`,
     `_make_source_range_struct`, and the `_LEGACY_REGISTRY_ATTRS`-based
     `__getattr__` to `mlody/evaluator/decoration.py`.
   - Replace the in-evaluator decoration call with a
     `value_decorator: Callable[[str, Named, SourceRange | None], Named] | None`
     hook on `Evaluator.__init__`.

4. **Pull out `resolve()`.**
   - Move the entire `Evaluator.resolve` method, plus its helpers, to
     `mlody/evaluator/resolve.py` as a free function taking an `Evaluator` (or a
     `MlodyEvaluator` wrapper).

5. **Pull out the `python.*` extras.**
   - Move `_parse_astropy_unit` / `_parse_quantity_string` /
     `_format_quantity_string` to `mlody/sandbox/units.py`.
   - Move `_uuid7_string` to `mlody/sandbox/uuids.py`.
   - Move the entire GitHub/HTTP block (`_GitHubContentTarget`,
     `_HTTP_INFO_USER_AGENT`, `_coerce_http_length`,
     `_normalize_http_update_time`, `_normalize_http_digest`,
     `_extract_http_digest`, `_http_headers_info`, `_github_request`,
     `_load_json`, `_parse_github_content_target`, `_github_contents_api_url`,
     `_github_commits_api_url`, `_extract_github_update_time`,
     `_github_http_info`, `_generic_http_info`, `_http_info`) to
     `mlody/sandbox/http_info.py`.
   - Move `_expand_commit_sha`, `_FULL_SHA_RE`, `_SHORT_SHA_RE` to
     `mlody/sandbox/git_helpers.py`.
   - Move `_runtime_json_data` / `_runtime_json_blob` and
     `_looks_like_workspace` to `mlody/cli/runtime_json.py`. Update
     `mlody/cli/server.py:32` import accordingly.
   - Slim `PYTHON_SPECIFIC_BUILTINS` down to the truly generic helpers
     (`hasattr`, `getattr`, `round`, `sum`, `re`, `hashlib`, `os`, `id`, `Any`,
     `Callable`). The mlody-side host wires up the extras via a new
     `Evaluator(..., python_extras: Mapping[str, Any] | None = None)` parameter.
   - Drop `@pip//astropy` and `@pip//uuid_utils` from
     `common/python/starlarkish/evaluator/BUILD.bazel`. Move them to
     `mlody/sandbox/BUILD.bazel`.

6. **Move `__MLODY__` and the multimethod hooks.**
   - Delete `sandbox_globals["__MLODY__"] = True`. If anything in `.mlody`
     scripts still relies on this, mlody can re-inject it via
     `_persistent_injections`.
   - Remove `register_method`, `get_methods`, `dispatch_method` from `Builtins`.
     Replace with a single mlody-side `Builtins.host_extras: Mapping[str, Any]`
     (or use `inject` directly).

7. **Move the primitive-type seeding.**
   - Delete the `for _pname in ["integer", "string", "bool", "float"]` loop from
     `Evaluator.__init__`. The mlody-side factory runs it after constructing the
     evaluator.

8. **Move tests.**
   - Move `evaluator_test.py` and `evaluator_generics_test.py` into
     `mlody/evaluator/` (or split — generic load/parse/sandbox/Struct tests
     stay; everything that mentions `root`/`task`/`value`/etc. moves). Update
     `BUILD.bazel` accordingly.

9. **Cosmetic genericisation.**
   - Update `STARLARK_DEVIATIONS.md` and the module docstring of `evaluator.py`
     to talk about "script files" rather than `.mlody` files, or parameterise
     via an `Evaluator(file_extension=".mlody")` constructor argument.
   - Rewrite `InMemoryFS` docstring.

### Directory shape after migration

- `common/python/starlarkish/`
  - `core/` — `Struct`, `struct` (unchanged)
  - `evaluator/`
    - `evaluator.py` — generic sandbox, load/exec, fork, install bindings
    - `registry.py` — generic `NamedRegistry` + `KindedRegistry`
    - `testing.py` — `InMemoryFS` (docstring rewritten)
- `mlody/evaluator/` (new)
  - `evaluator.py` — mlody-flavoured wrapper / factory
  - `registry.py` — `make_mlody_registry()` + typed accessors
  - `decoration.py` — entity-type decoration, virtual values, methods, source
    ranges
  - `resolve.py` — action/task port resolution
  - `bootstrap.py` — primitive-type seeding
- `mlody/sandbox/` (new)
  - `units.py`, `uuids.py`, `http_info.py`, `git_helpers.py`
- `mlody/cli/runtime_json.py` (new) — `_runtime_json_data` /
  `_runtime_json_blob` / `_looks_like_workspace`
- `common/python/console/` — unchanged, already clean
