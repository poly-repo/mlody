# Tasks: mlody-code-review-remediation

This plan is the implementation blueprint for the findings documented in
`CODE_REVIEW.md`.

## Assumptions

- Preserve current user-visible behavior for `mlody show`, `mlody dag`,
  workspace resolution, parquet/SQL-backed value display, and the Hugging Face
  downloader CLI unless a task explicitly states otherwise.
- Keep existing Bazel target names stable where practical, especially
  `//mlody/common/huggingface:model-download` and existing `o_py_test` targets.
- Do not modify `common/python/starlarkish/...`; all abstraction work stays
  inside `mlody/`.
- Prefer additive seams first, then migrate call sites, then remove duplicated
  helpers once tests prove parity.

## Sequencing

- Task 1 is a prerequisite for every other task.
- Tasks 2 and 3 must land before Task 4 and Task 5.
- Task 5 may begin after Task 2 if the typed location adapters are introduced
  first, but it must not delete old `show.py` branches until Task 1 regression
  tests pass.
- Task 6 may run after Task 1, but if it changes `cli/show.py` it must rebase on
  top of Task 5 to avoid helper churn.
- Task 7 is independent of the core refactors except for shared test and lint
  expectations.

---

## Task 1 — Add characterization coverage before refactoring

- [x] 1.1 Extend `mlody/cli/show_test.py` with characterization tests covering:
      plain parquet preview, derived-value preview, derived scalar-shape error,
      unreadable parquet fallback to DOM rendering, and DAG preview rendering for
      output labels
- [x] 1.2 Extend `mlody/core/workspace_test.py` with characterization tests for
      the currently supported anchor-resolution cases:
      workspace attribute, registry entity, root object, module global,
      module aggregate, and root collection
- [x] 1.3 Add or extend tests proving current "list of named values" traversal
      semantics remain unchanged in both `mlody/core/virtual_value.py` and
      `mlody/core/workspace.py`
- [x] 1.4 Extend `mlody/core/setf_test.py` with characterization cases for
      `FieldSegment`, `IndexSegment`, `KeySegment`, `SliceSegment`,
      wildcard expansion, recursive descent, and lineage propagation on the
      current implementation
- [x] 1.5 Extend `mlody/common/huggingface/model_download_test.py` with a
      characterization test proving `model-download.py <repo> ...` still works
      without an explicit subcommand
- [x] 1.6 Run and record the baseline test set:
      `bazel test //mlody/core:workspace_test //mlody/core:setf_test //mlody/core:derived_test //mlody/core/sql:sql_test //mlody/core/parquet:parquet_test //mlody/cli:show_test //mlody/common/huggingface:model_download_test`

Status: [x]

---

## Task 2 — Strengthen `Struct` so callers stop rebuilding it by hand

- [x] 2.1 Extend `mlody/common/struct.py` with a small, explicit immutable API:
      `get(name, default)`, `items()`, and `updated(**changes) -> Struct`
- [x] 2.2 Keep `to_dict()` and `as_mapping()` unchanged for backward
      compatibility; add tests for the new helpers in a new
      `mlody/common/struct_test.py`
- [x] 2.3 Update `mlody/common/BUILD.bazel` to add an `o_py_test` target for
      `struct_test.py`
- [x] 2.4 Migrate `mlody/core/lineage.py` to use `Struct.updated(...)` instead of
      copying `as_mapping()` into an ad hoc dict and reconstructing the `Struct`
- [x] 2.5 Migrate `mlody/core/setf_strategies.py` and the `Struct`-rebuilding
      branches in `mlody/core/setf.py` to use `Struct.updated(...)`
- [x] 2.6 Migrate `mlody/core/workspace.py` port-conversion code to stop reading
      `entity._fields` directly; use public `Struct` helpers only
- [x] 2.7 Verify no remaining production code in `cli/`, `common/`, or `core/`
      reads `Struct._fields` directly
- [x] 2.8 Run `bazel test //mlody/common:struct_test //mlody/core:lineage_test //mlody/core:setf_test //mlody/core:workspace_test`

Status: [x]

---

## Task 3 — Introduce a shared traversal/runtime access layer

- [x] 3.1 Create `mlody/core/traversal_runtime.py` with an explicit adapter-based
      API for runtime access:
      `step_named_child(obj, name)`, `step_segment(obj, segment)`,
      `iter_children(obj)`, and `replace_child(obj, segment, new_child)`
- [x] 3.2 Define a small `TraversalAdapter` protocol in
      `traversal_runtime.py`; implement adapters for `Struct`, `list`, `dict`,
      and virtual values
- [x] 3.3 Preserve current list-by-name semantics by implementing a named-child
      lookup path for sequences whose elements expose `.name`
- [x] 3.4 Migrate `Workspace._step_resolved_object` in
      `mlody/core/workspace.py` to delegate to `step_named_child(...)`
- [x] 3.5 Migrate `step_object(...)` in `mlody/core/virtual_value.py` to
      delegate to the same helper, removing the duplicated
      "list-by-name else getattr" branch
- [x] 3.6 Migrate `mlody/core/setf.py` helpers `_step`, `_children`,
      `_replace_one`, and `_has_lineage` to use `traversal_runtime.py`
- [x] 3.7 Migrate `mlody/core/setf_strategies.py` preflight and commit logic to
      reuse the shared replacement/access layer instead of duplicating container
      checks inline
- [x] 3.8 Add `mlody/core/traversal_runtime_test.py` covering:
      named-child lookup on lists, struct field access, dict key access,
      list index access, slice expansion via `setf`, child iteration, and
      replacement semantics
- [x] 3.9 Update `mlody/core/BUILD.bazel` with `traversal_runtime.py` and
      `traversal_runtime_test.py`, wiring deps into `workspace_lib`, `setf`,
      and `core_lib`
- [x] 3.10 Run `bazel test //mlody/core:traversal_runtime_test //mlody/core:setf_test //mlody/core:workspace_test //mlody/core:place_test`

Status: [x]

---

## Task 4 — Split `Workspace` into loader, registry view, and anchor resolver

- [x] 4.1 Create `mlody/core/workspace_loader.py` and move Phase 1, Phase 2, and
      Phase 3 loading orchestration out of `Workspace.load()` while preserving
      the public `Workspace.load(verbose: bool = False) -> None` entry point
- [x] 4.2 Create `mlody/core/registry_view.py` as the only module allowed to
      touch evaluator internals such as `_roots_by_name`, `_module_globals`,
      `loaded_files`, `_types_by_name`, and `all`
- [x] 4.3 Create `mlody/core/anchor.py` defining an `Anchor` protocol and the
      concrete anchor types currently encoded by `writeback_kind`:
      `WorkspaceAttributeAnchor`, `RegistryEntityAnchor`, `RootObjectAnchor`,
      `ModuleGlobalAnchor`, `ModuleAggregateAnchor`, and `RootCollectionAnchor`
- [x] 4.4 Refactor `Workspace.resolve_label_anchor(...)` so it returns concrete
      anchor objects instead of a stringly `writeback_kind` discriminator
- [x] 4.5 Preserve `LabelWriteAnchor` compatibility for existing tests and
      call sites only if needed; otherwise replace it with the new anchor model
      and update callers in `mlody/core/setf.py`
- [x] 4.6 Move registry matching and wildcard expansion logic out of
      `Workspace` into `registry_view.py` or a dedicated resolver helper so
      `Workspace` becomes a thin facade over services
- [x] 4.7 Keep `Workspace.evaluator`, `Workspace.root_infos`, and
      `Workspace.info` behavior stable; update tests only where the internal
      factoring changes
- [x] 4.8 Add focused tests for each anchor class and for `WorkspaceLoader`
      failure aggregation; keep existing `workspace_test.py` as the integration
      safety net
- [x] 4.9 Update `mlody/core/BUILD.bazel` with the new modules and their tests
- [x] 4.10 Run `bazel test //mlody/core:workspace_test //mlody/core:setf_test //mlody/cli:show_test //mlody/cli:shell_test`

Status: [x]

---

## Task 5 — Replace ad hoc parquet/SQL/derived branching with a tabular source abstraction

- [x] 5.1 Create a new `mlody/core/tabular/` package with:
      `__init__.py`, `interfaces.py`, `parquet_source.py`,
      `derived_source.py`, and `location_specs.py`
- [x] 5.2 Define the typed primitives in `interfaces.py`:
      `QuerySpec`, `PreviewResult`, and a `TabularSource` protocol with
      `preview(limit: int)`, `count()`, and `materialize()`
- [x] 5.3 Implement `ParquetSource` in `parquet_source.py` as the typed wrapper
      for one or more parquet paths; move path normalization and schema probing
      out of `cli/show.py` and into this type
- [x] 5.4 Implement `DerivedSource` in `derived_source.py` as the typed wrapper
      for derived queries and their materialized cache path; keep `mlody_query`
      as the low-level query engine
- [x] 5.5 Implement typed location adapters in `location_specs.py` for the
      currently supported location shapes used by the Python runtime:
      at minimum `PosixLocationSpec` and `DerivedLocationSpec`
- [x] 5.6 Refactor `mlody/core/derived.py` so `materialise_derived(...)` accepts
      a typed derived spec or `DerivedSource` instead of a generic `location`
      object plus raw `source_paths`
- [x] 5.7 Refactor `mlody/core/location_composition.py` to construct and
      serialize typed location specs instead of rebuilding raw `attributes`
      dicts inline for derived locations
- [x] 5.8 Refactor `mlody/cli/show.py` to replace:
      `location.type == "derived"`,
      `_source_paths_from_location(...)`,
      source-path coercion,
      `SELECT COUNT(*)`,
      and direct `pq.read_table(...)`
      with a `TabularSource` factory and the new typed API
- [x] 5.9 Keep `mlody/core/parquet/deserializer.py` as the low-level file reader
      used by traversal and tests; do not make it responsible for CLI behavior
      or derived-query orchestration
- [x] 5.10 Add `mlody/core/tabular/*_test.py` covering:
       parquet preview, row counting, derived cache hit, derived cache miss,
       scalar-shape rejection, schema diagnostics, and source-from-location
       parsing for both plain and derived values
- [x] 5.11 Update `mlody/core/BUILD.bazel`, `mlody/core/sql/BUILD.bazel`, and
       `mlody/cli/BUILD.bazel` to include the new package and any refactored deps
- [x] 5.12 Run `bazel test //mlody/core/sql:sql_test //mlody/core/parquet:parquet_test //mlody/core:derived_test //mlody/core:location_composition_test //mlody/cli:show_test`

Status: [x]

---

## Task 6 — Deduplicate DAG selection and rendering logic across `show` and `dag`

- [x] 6.1 Create `mlody/cli/dag_render.py` with shared helpers for:
      short type names, value-list formatting, action-cell formatting, DAG table
      rendering, and label-to-subgraph selection
- [x] 6.2 Introduce a small typed result object in `dag_render.py` for label
      resolution outcomes, carrying at minimum `graph`, `resolved_label`, and
      any suggestion text needed by `dag_cmd.py`
- [x] 6.3 Keep the two command behaviors distinct:
      `mlody dag` must continue to accept task labels and output-port labels,
      while `mlody show` must continue to render only the output-label ancestor
      preview when applicable
- [x] 6.4 Remove the duplicate `_short_type_name`, `_format_value_list`,
      `_format_action_cell`, and DAG table-building code from both
      `mlody/cli/show.py` and `mlody/cli/dag_cmd.py`
- [x] 6.5 Add focused unit coverage for `dag_render.py`, and keep
      `show_test.py` and `dag_cmd_test.py` as command-level regression tests
- [x] 6.6 Update `mlody/cli/BUILD.bazel` to add `dag_render.py` to `cli_lib`
- [x] 6.7 Run `bazel test //mlody/cli:dag_cmd_test //mlody/cli:show_test //mlody/cli:main_test`

Status: [x]

---

## Task 7 — Modularize `common/huggingface/model-download.py` without breaking the existing entrypoint

- [x] 7.1 Create a proper package split under `mlody/common/huggingface/`:
      `repo_types.py`, `repo_client.py`, `resume_state.py`, `download.py`,
      and `cli.py`
- [x] 7.2 Introduce `RepoType(Enum)` with `MODEL` and `DATASET` values; replace
      the current stringly `repo_type` branching in production code with the enum
- [x] 7.3 Move partial-download metadata validation and persistence out of the
      entrypoint module and into `resume_state.py`
- [x] 7.4 Move segmented-download transport logic into `download.py`, with typed
      helpers for bandwidth estimation, segment planning, and file download
- [x] 7.5 Move Hugging Face API and metadata lookups (`model_info`,
      `dataset_info`, `HfApi.list_repo_refs`, `hf_hub_url`) into `repo_client.py`
- [x] 7.6 Keep `model-download.py` as a thin compatibility shim that delegates to
      `cli.py` so the existing Bazel `o_py_binary`, `o_py_library`, and tests do
      not need a behavioral rename
- [x] 7.7 Preserve backward-compatible CLI behavior:
      `model-download.py <repo> ...` still implies the `download` subcommand,
      and `--dataset` still selects dataset semantics
- [x] 7.8 Update `common/huggingface/BUILD.bazel` so the library target depends
      on the new modules rather than treating `model-download.py` as the entire
      implementation
- [x] 7.9 Expand `model_download_test.py` to cover the new thin-entrypoint path
      plus unit-level behavior for `RepoType`, resume metadata validation, and
      repo-client dispatch
- [x] 7.10 Run `bazel test //mlody/common/huggingface:model_download_test`

Status: [x]

---

## Task 8 — Final cleanup, compatibility verification, and lint

- [x] 8.1 Remove dead compatibility helpers only after all migrated callers are
      on the new abstractions and characterization tests remain green
- [x] 8.2 Verify no remaining duplicated "list-by-name else getattr" production
      logic exists in `cli/`, `common/`, or `core/`
- [x] 8.3 Verify no remaining production branches in `cli/show.py` or
      `core/location_composition.py` inspect raw location `type` strings to
      decide parquet-vs-derived behavior
- [x] 8.4 Run the focused regression suite:
      `bazel test //mlody/core/sql:sql_test //mlody/core/parquet:parquet_test //mlody/core:derived_test //mlody/core:location_composition_test //mlody/core:workspace_test //mlody/core:setf_test //mlody/core:dag_test //mlody/cli:show_test //mlody/cli:dag_cmd_test //mlody/cli:shell_test //mlody/common/huggingface:model_download_test`
- [x] 8.5 Run `bazel test //mlody/...`
- [x] 8.6 Run `bazel build --config=lint //mlody/...` and fix all new lint or
      type-check issues introduced by the refactor
- [x] 8.7 Update `CODE_REVIEW.md` or add a short follow-up note only if the final
      implemented design intentionally diverges from this remediation plan

Status: [x]
