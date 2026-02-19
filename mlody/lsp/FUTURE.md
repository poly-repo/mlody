# MLody LSP — Future Work

Deferred features and known limitations captured from the `lsp-server` design at the time of the MVP implementation.

---

## Features Explicitly Deferred (Non-Goals for MVP)

### Incremental re-evaluation on file change

The `Workspace` is loaded once during the `initialize` handshake and never refreshed. If a `.mlody` file changes on disk, the user must restart the language server to see updated completions and definitions.

**Approach when tackling:** Watch `evaluator.loaded_files` for modifications, invalidate the relevant entry in `_module_globals`, and re-evaluate only the changed file and its dependents. Requires async file watching (e.g., `watchfiles`) and careful state management to avoid races with in-flight LSP requests.

---

### Additional LSP capabilities

Only `textDocument/completion` and `textDocument/definition` are implemented. The following standard capabilities are absent:

- **Hover** — show type / value information for a symbol under the cursor
- **Diagnostics** — surface evaluation errors (syntax, undefined symbols) as editor squiggles
- **Rename** — rename a symbol across all files that import it
- **References** — find all files that `load()` a given symbol or file

---

### Completion / definition for Python builtins outside `SAFE_BUILTINS`

Only names in `SAFE_BUILTINS` are offered as completions. Standard Python builtins that are not explicitly allow-listed (e.g., `dict`, `list`, `int`) are invisible to the LSP.

---

### Transitive import navigation

Go-to-definition only follows symbols that are directly listed in the current file's own `load()` call. If `helper.mlody` re-exports `SHARED_VAR` from `base.mlody`, and the current file loads from `helper.mlody` without explicitly naming `SHARED_VAR`, the server returns no result.

**Approach when tackling:** Walk the `_module_globals` of each directly-imported file to find the symbol's origin, then recurse until the defining file is reached.

---

## Technical Debt / Clean-up

### Add a public `Evaluator.get_module_globals(path)` accessor

`completion.py` and `definition.py` access `evaluator._module_globals` directly — a private implementation detail. If the evaluator refactors its internals the LSP breaks silently.

**Task:** Add a `get_module_globals(path: Path) -> dict[str, Any]` method to `Evaluator` and update both provider modules to use it instead of the private attribute.

**Reference:** `design.md` Decision 4, Risk 1.

---

### Drive the server name/version from a shared constant

`server.py` hard-codes `"mlody-lsp"` and `"v0.1"`. These should be derived from a package-level version constant (e.g., `mlody.__version__`) once versioning is established.

**Reference:** `design.md` Open Questions.

---

## Known Limitations (Documented, Not Bugs)

### Stale completions after file edits

Because the workspace is never re-evaluated, completions and definitions reflect the state of `.mlody` files at server start time. **Workaround:** restart the language server after editing pipeline files.

### Multi-line `load()` calls confuse context detection

Context detection (completion source selection) examines only the current line up to the cursor position using a single regex. A `load()` call split across multiple lines (e.g., path string on one line, symbol list on the next) will not be recognised as a load-path context.

**Approach when tackling:** Replace the single-line regex with a small backward-scan over the document buffer up to the opening `load(`.
