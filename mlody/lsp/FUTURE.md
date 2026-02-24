# MLody LSP — Future Work

Deferred features and known limitations captured from the `lsp-server` design at
the time of the MVP implementation.

---

## Features Explicitly Deferred (Non-Goals for MVP)

### Incremental re-evaluation on file change

The `Workspace` is loaded once during the `initialize` handshake and never
refreshed. If a `.mlody` file changes on disk, the user must restart the
language server to see updated completions and definitions.

**Approach when tackling:** Watch `evaluator.loaded_files` for modifications,
invalidate the relevant entry in `_module_globals`, and re-evaluate only the
changed file and its dependents. Requires async file watching (e.g.,
`watchfiles`) and careful state management to avoid races with in-flight LSP
requests.

---

### Smarter on-save workspace reload

`on_changed_watched_files` currently performs a full workspace teardown and
reload (`Workspace(...).load()`) whenever any `.mlody` file changes on disk.
This is correct but potentially slow for large workspaces.

**Why deferred:** A full reload is safe and simple because `.mlody` files
reference each other via `load()`, making any single change potentially affect
transitive dependents. Incremental invalidation requires a file-level dependency
graph that does not currently exist. The full-reload behaviour is intentionally
kept for this release of the incremental-sync feature.

**Approach when tackling:** Build a dependency graph from the `load()` calls
extracted by `get_load_statements()`. On a file-change event, walk the graph
forward from the changed file to find all transitive dependents. Re-evaluate
only those files in dependency order, updating `_module_globals` entries in
place. Requires careful locking or a copy-on-write strategy to avoid serving
stale state to in-flight LSP requests during reload.

---

### Cross-file unsaved buffer completions

**Context:** The incremental-sync feature (delivered in this release) improves
completions for symbols defined in the _current_ file's unsaved buffer by
extracting top-level bindings directly from the tree-sitter parse tree. It does
_not_ address symbols from _other_ open, unsaved files.

Today, when a user edits `helper.mlody` in a second editor tab without saving,
and the current file does `load("//mlody/helper.mlody", "NEW_SYMBOL")`, the
server returns no completion for `NEW_SYMBOL` — because the starlarkish
evaluator only knows the last on-disk state of `helper.mlody`.

**The architectural tradeoff:**

There are three possible approaches for cross-file unsaved buffer completions,
each with different implications for the growing feature set:

1. **Re-run starlarkish on the unsaved buffer text.** The server would maintain
   a per-URI in-memory text store (already present after the incremental-sync
   feature), and on each `didChange` for any open file, attempt to re-evaluate
   that file's text through the starlarkish evaluator. This yields rich,
   evaluated symbol _values_ (Struct fields, function results, etc.) and
   composes well with value-aware hover and future type-inference features. The
   risk is that starlarkish evaluation fails on partial edits (mid-keystroke
   syntax errors), requiring a fallback to last-known-good state per file.

2. **Fall back to tree-sitter-only name extraction for unsaved other-files.**
   When another open file has unsaved changes, extract only symbol _names_
   (identifiers at top scope, same logic as same-file extraction) from its
   in-memory parse tree. This is simpler and never fails on syntax errors, but
   yields only names without values — increasingly insufficient as the feature
   set grows to include value-aware hover, type information, and Struct field
   completions.

3. **Keep last-known-good starlarkish state (current behaviour).** No change.
   Cross-file completions remain stale until the other file is saved. Simple,
   correct, but degrades the editing experience for multi-file workflows.

**Recommendation when tackling:** Option 1 (starlarkish re-evaluation on unsaved
buffers) is the right long-term direction. Implement it with a per-URI
"last-good-globals" fallback: attempt re-evaluation on each `didChange`, update
the globals cache on success, keep the previous globals on failure. This
requires extending `DocumentCache` to track evaluated globals per URI alongside
parse trees, decoupling per-file globals from the workspace-level `Evaluator`
instance.

---

### Additional LSP capabilities

Only `textDocument/completion` and `textDocument/definition` are implemented.
The following standard capabilities are absent:

- **Hover** — show type / value information for a symbol under the cursor
- **Diagnostics** — surface evaluation errors (syntax, undefined symbols) as
  editor squiggles
- **Rename** — rename a symbol across all files that import it
- **References** — find all files that `load()` a given symbol or file

---

### Completion / definition for Python builtins outside `SAFE_BUILTINS`

Only names in `SAFE_BUILTINS` are offered as completions. Standard Python
builtins that are not explicitly allow-listed (e.g., `dict`, `list`, `int`) are
invisible to the LSP.

---

### Transitive import navigation

Go-to-definition only follows symbols that are directly listed in the current
file's own `load()` call. If `helper.mlody` re-exports `SHARED_VAR` from
`base.mlody`, and the current file loads from `helper.mlody` without explicitly
naming `SHARED_VAR`, the server returns no result.

**Approach when tackling:** Walk the `_module_globals` of each directly-imported
file to find the symbol's origin, then recurse until the defining file is
reached.

---

## Technical Debt / Clean-up

### Add a public `Evaluator.get_module_globals(path)` accessor

`completion.py` and `definition.py` access `evaluator._module_globals` directly
— a private implementation detail. If the evaluator refactors its internals the
LSP breaks silently.

**Task:** Add a `get_module_globals(path: Path) -> dict[str, Any]` method to
`Evaluator` and update both provider modules to use it instead of the private
attribute.

**Reference:** `design.md` Decision 4, Risk 1.

---

### Drive the server name/version from a shared constant

`server.py` hard-codes `"mlody-lsp"` and `"v0.1"`. These should be derived from
a package-level version constant (e.g., `mlody.__version__`) once versioning is
established.

**Reference:** `design.md` Open Questions.

---

## Known Limitations (Documented, Not Bugs)

### Stale completions after file edits

Because the workspace is never re-evaluated, completions and definitions reflect
the state of `.mlody` files at server start time. **Workaround:** restart the
language server after editing pipeline files.

### Multi-line `load()` calls confuse context detection

Context detection (completion source selection) examines only the current line
up to the cursor position using a single regex. A `load()` call split across
multiple lines (e.g., path string on one line, symbol list on the next) will not
be recognised as a load-path context.

**Approach when tackling:** Replace the single-line regex with a small
backward-scan over the document buffer up to the opening `load(`.
