# Requirements Document: mlody LSP — Incremental Updates

**Version:** 1.0 **Date:** 2026-02-23 **Prepared by:** Requirements Analyst AI
**Status:** Draft

---

## 1. Executive Summary

The mlody LSP server currently uses `TextDocumentSyncKind.Full` synchronisation,
meaning the client sends the complete document text on every keystroke.
Completion and go-to-definition results reflect only the last on-disk state of
each `.mlody` file: symbols defined in the current unsaved buffer are invisible
to the completion engine until the file is saved.

This document specifies the requirements for two tightly coupled improvements:

1. **LSP sync mode change** — switch from `TextDocumentSyncKind.Full` to
   `TextDocumentSyncKind.Incremental` so the client sends only text range diffs
   on `textDocument/didChange`. The server applies those diffs to a maintained
   document buffer and passes the previous parse tree to tree-sitter's
   incremental re-parse API, avoiding a full re-parse on every keystroke.

2. **Same-file unsaved symbol extraction** — after each incremental re-parse,
   extract top-level symbol names from the updated parse tree (completed
   assignments and `def` statements only, excluding ERROR subtrees) and use them
   to augment completion results for the current buffer, even before the file is
   saved to disk.

The expected business value is that users can autocomplete symbols they have
just typed in the current file without needing to save first, and the server
remains responsive during fast typing on large files.

Cross-file unsaved buffer completions (symbols defined in another open, unsaved
file) are explicitly out of scope for this release; see Section 2.2 and
`FUTURE.md` for the deferred design.

---

## 2. Project Scope

### 2.1 In Scope

- Change `TextDocumentSyncKind.Full` to `TextDocumentSyncKind.Incremental` in
  the pygls server initialisation.
- Implement diff application logic: on `textDocument/didChange`, apply each
  `TextDocumentContentChangePartial` range-edit to the maintained document
  buffer to produce the new full text.
- Pass the previous parse tree (`old_tree`) to `tree_sitter.Parser.parse()` on
  every re-parse, enabling tree-sitter's incremental re-parse optimisation.
- Implement same-file top-level symbol extraction from the current parse tree:
  walk completed assignment targets and `def` statement names at file scope,
  excluding any node that is inside or immediately adjacent to an ERROR subtree.
- Augment the `general` completion context in `completion.py` to merge same-file
  extracted symbols with the existing evaluator globals for the current file.
- Maintain full correctness of all existing LSP features (completion,
  go-to-definition, hover, diagnostics, semantic tokens) under incremental sync.
- Update `DocumentCache` (in `parser.py`) to store the full document text
  alongside each `(version, Tree)` pair, so that diff application has access to
  the previous buffer state.

### 2.2 Out of Scope

- **Cross-file unsaved buffer completions** — symbols from another open, unsaved
  `.mlody` file will continue to reflect the last on-disk evaluated state. See
  `FUTURE.md` for the deferred design and architectural tradeoff.
- **Workspace re-evaluation on save** — `on_changed_watched_files` performs a
  full workspace reload; this behaviour is unchanged. See `FUTURE.md` for a note
  on future reconsidering.
- **Dependency graph construction** — no file-level dependency graph between
  `.mlody` files is built as part of this feature.
- **Incomplete symbol extraction** — symbols from incomplete or syntactically
  broken assignments (e.g., `MY_MODEL = struct(` with no closing paren) are not
  extracted. Only syntactically complete top-level bindings contribute to
  completions.
- **Value-aware completions from the unsaved buffer** — tree-sitter extraction
  yields symbol names only, not evaluated values. Hover display of
  `Value: repr(v)` continues to read from the starlarkish evaluator globals
  (last saved state).

### 2.3 Assumptions

- The client (Eglot or equivalent) correctly advertises
  `TextDocumentSyncKind.Incremental` support and sends well-formed
  `TextDocumentContentChangePartial` objects with `range` and `text` fields.
- tree-sitter's incremental re-parse (`parser.parse(bytes, old_tree=tree)`) is
  safe to call from the single asyncio event-loop thread that pygls uses for all
  handlers; no additional locking is required.
- The `.mlody` grammar is Starlark; top-level scope means statements that are
  direct children of the root `module` node in the tree-sitter parse tree.
- UTF-8 byte offsets and LSP character offsets coincide for all practical
  `.mlody` content (ASCII-safe Starlark). No UTF-16 surrogate pair handling is
  required.

### 2.4 Constraints

- Python 3.13.2, hermetic via `rules_python`.
- All code must pass `basedpyright` strict mode.
- All code must be formatted with `ruff`.
- Bazel build rules: `o_py_library`, `o_py_binary`, `o_py_test` from
  `//build/bzl:python.bzl`.
- No new third-party dependencies may be introduced. The diff application logic
  must be implemented using the `lsprotocol` types already in scope
  (`TextDocumentContentChangePartial`) and stdlib only.

---

## 3. Stakeholders

| Role                | Name/Group                   | Responsibilities                                     |
| ------------------- | ---------------------------- | ---------------------------------------------------- |
| Primary users       | mlody pipeline authors       | Edit `.mlody` files in an LSP-enabled editor (Eglot) |
| Maintainers         | Polymath Solutions engineers | Review, extend, and operate the LSP server           |
| Requirements author | Requirements Analyst AI      | Elicit and document requirements                     |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Completion results for the current file must reflect the in-memory
  buffer state, not the last saved state, so that newly typed symbols are
  immediately available for autocomplete without a save-and-wait cycle.
- **BR-002:** The server must remain responsive during fast typing; incremental
  re-parse must not introduce perceptible lag compared to the current full
  re-parse.

### 4.2 Success Metrics

- **KPI-001:** A symbol defined by a completed top-level assignment or `def`
  statement in the current unsaved buffer appears in completion candidates
  within one `textDocument/didChange` + `textDocument/completion` round trip.
  Measurement: automated test asserting the completion list contains the
  newly-defined symbol before any save event.
- **KPI-002:** Completion response latency (from `textDocument/didChange` to
  `textDocument/completion` response) is at or below 200 ms for a 500-line
  `.mlody` file on developer hardware. Measurement: manual timing or benchmark
  test.
- **KPI-003:** No existing passing test is broken by this change. Measurement:
  `bazel test //mlody/lsp/...` green after implementation.

---

## 5. User Requirements

### 5.1 User Persona

**Pipeline Author**

A data engineer writing `.mlody` pipeline definitions in Emacs with Eglot. They
define new Starlark bindings at the top of a file and immediately want those
names to autocomplete further down in the same file, without interrupting their
flow to save.

Pain points today:

- Types `DATASET = struct(path="...", format="csv")` on line 5.
- Moves to line 20 and types `DATASE` — no completion for `DATASET` appears
  because the file has not been saved yet and the evaluator has not re-run.
- Must save, wait for the file watcher to fire, workspace to reload, then retry.

### 5.2 User Stories

**Epic 1: Live same-file completions**

- **US-001:** As a pipeline author, I want symbols I have just defined in the
  current file to appear as completion candidates immediately, without saving,
  so that I can author pipelines without interrupting my editing flow.
  - Acceptance Criteria:
    - Given I have typed `MY_MODEL = struct(name="bert")` on line 2 of an open
      `.mlody` buffer (file not saved),
    - When I trigger completion on `MY_` at line 10,
    - Then `MY_MODEL` appears in the completion list.
  - Priority: Must Have

- **US-002:** As a pipeline author, I want completions to remain stable and
  correct while I am mid-edit (partial syntax), so that the editor does not show
  spurious or misleading suggestions.
  - Acceptance Criteria:
    - Given I have typed `MY_MODEL = struct(` (incomplete — parse error) on line
      2,
    - When I trigger completion on `MY_` at line 10,
    - Then `MY_MODEL` does NOT appear in the completion list (incomplete
      assignment does not contribute symbols).
  - Priority: Must Have

**Epic 2: Incremental sync correctness**

- **US-003:** As a pipeline author, I want all existing LSP features
  (go-to-definition, hover, diagnostics, semantic tokens) to continue working
  correctly after switching to incremental sync mode, so that no capability
  regresses.
  - Acceptance Criteria:
    - Given a `.mlody` file open in the editor,
    - When I make a series of edits without saving,
    - Then go-to-definition, hover, diagnostics, and semantic tokens all reflect
      the current buffer state, identical to their behaviour under Full sync.
  - Priority: Must Have

---

## 6. Functional Requirements

### 6.1 LSP Sync Mode

**FR-001: Switch to Incremental sync**

- Description: Change the `LanguageServer` constructor argument from
  `text_document_sync_kind=types.TextDocumentSyncKind.Full` to
  `text_document_sync_kind=types.TextDocumentSyncKind.Incremental`.
- Inputs: LSP `initialize` handshake.
- Processing: pygls advertises `Incremental` sync capability to the client.
- Outputs: Client sends `TextDocumentContentChangePartial` (with `range` and
  `text` fields) on `textDocument/didChange` instead of the full document text.
- Business Rules: None.
- Priority: Must Have
- Dependencies: FR-002 (diff application must be in place before this change is
  safe to deploy).

**FR-002: Diff application on didChange**

- Description: On `textDocument/didChange`, apply each
  `TextDocumentContentChangePartial` range edit to the maintained document
  buffer in order to reconstruct the full document text after each change.
- Inputs: `DidChangeTextDocumentParams.content_changes` — a list of
  `TextDocumentContentChangePartial` objects, each carrying `range` (start/end
  line+character) and `text` (the replacement string).
- Processing:
  1. Retrieve the current full text for the URI from `DocumentCache`.
  2. For each change in order: split the current text into lines, apply the
     range replacement (replacing characters from `range.start` to `range.end`
     with `change.text`), and reassemble.
  3. The resulting text is the new full document content.
- Outputs: Updated full document text, passed to `DocumentCache.update()` for
  re-parsing.
- Business Rules:
  - Changes must be applied in the order they appear in `content_changes`.
  - If a change carries no `range` field (i.e., it is a
    `TextDocumentContentChangeWholeDocument`), treat it as a full-text
    replacement (defensive fallback).
- Priority: Must Have
- Dependencies: FR-003 (DocumentCache must store text alongside trees).

**FR-003: DocumentCache stores document text**

- Description: Extend `DocumentCache` to store the full document text alongside
  each `(version, Tree)` pair, so that diff application (FR-002) can retrieve
  the previous buffer contents by URI.
- Inputs: `DocumentCache.update(uri, version, text)` — unchanged signature.
- Processing: Store `(version, text, tree)` triples keyed by URI. Expose a
  `get_text(uri) -> str | None` accessor.
- Outputs: `get_text(uri)` returns the last-stored full text for `uri`, or
  `None` if the URI is not cached.
- Business Rules: The stored text is always the post-parse full text (after diff
  application, before the next change).
- Priority: Must Have
- Dependencies: None.

### 6.2 Incremental Re-parse

**FR-004: Pass old_tree to tree-sitter on re-parse**

- Description: When `DocumentCache.update()` re-parses a document, pass the
  previously cached `tree_sitter.Tree` as the `old_tree` keyword argument to
  `_parser.parse()`, enabling tree-sitter's incremental re-parse optimisation.
- Inputs: New document text (bytes), previous `tree_sitter.Tree` from cache.
- Processing: Call `_parser.parse(new_bytes, old_tree=previous_tree)` when a
  previous tree exists. Call `_parser.parse(new_bytes)` on first parse (no
  previous tree).
- Outputs: Updated `tree_sitter.Tree`.
- Business Rules: The `old_tree` argument is advisory for tree-sitter
  performance only; correctness does not depend on it. Passing a stale
  `old_tree` produces a correct (but potentially slower) parse.
- Priority: Must Have
- Dependencies: FR-003.

### 6.3 Same-file Symbol Extraction

**FR-005: Extract top-level symbols from the current parse tree**

- Description: After each re-parse of the current document, extract the names of
  all completed top-level bindings from the parse tree and make them available
  to the completion engine for the current file.
- Inputs: `tree_sitter.Tree` for the current document.
- Processing:
  1. Iterate over direct children of the root `module` node.
  2. For each `assignment` node: check that the node does not have
     `has_error == True` and is not inside an ERROR subtree. If clean, extract
     the left-hand side `identifier` child (index 0) as a symbol name.
  3. For each `function_definition` node: check that the node does not have
     `has_error == True`. If clean, extract the function name `identifier`
     (children[1]) as a symbol name.
  4. Return the collected names as a list of strings.
- Outputs: `list[str]` of symbol names defined in the current buffer.
- Business Rules:
  - Only top-level scope (direct children of `module`). Nested functions or
    assignments inside `if`/`for` blocks are not extracted.
  - Nodes with `has_error == True` or of type `ERROR` are skipped entirely,
    including their children.
  - Names that begin with `_` are excluded (private by convention).
  - Names already present in `_FRAMEWORK_INTERNALS` are excluded.
- Priority: Must Have
- Dependencies: FR-004.

**FR-006: Merge extracted symbols into general completions**

- Description: In `completion.py`, the `_general_completions` function must
  merge the tree-extracted symbol names (FR-005) with the existing evaluator
  globals for the current file, deduplicating by name.
- Inputs: `Evaluator | None`, `current_file: Path`, `tree: tree_sitter.Tree`.
- Processing:
  1. Collect names from `evaluator._module_globals.get(current_file, {})` as
     today (last saved state).
  2. Collect names from `extract_top_level_symbols(tree)` (FR-005).
  3. Union the two sets, excluding `_FRAMEWORK_INTERNALS` and `_`-prefixed
     names, deduplicated.
  4. Return as `list[CompletionItem]`.
- Outputs: `list[CompletionItem]` containing both saved-state and unsaved buffer
  symbols.
- Business Rules:
  - Tree-extracted names take precedence over nothing — they are additive. If a
    name appears in both the evaluator globals and the tree extraction, it
    appears once in the result.
  - If `evaluator` is `None` (workspace failed to load), tree-extracted names
    are still returned (the evaluator path is simply empty).
- Priority: Must Have
- Dependencies: FR-005.

### 6.4 Diagnostics

**FR-007: Diagnostics remain accurate under incremental sync**

- Description: The `_publish_diagnostics_for` pipeline (parse diagnostics + eval
  diagnostics) must produce identical results under incremental sync as under
  full sync. The input to `get_parse_diagnostics` is always the
  fully-reconstructed post-diff parse tree; no special handling is required.
- Inputs: Reconstructed full text after diff application; updated parse tree.
- Processing: Unchanged from current implementation.
- Outputs: `PublishDiagnosticsParams` reflecting the current buffer state.
- Business Rules: Diagnostics are published on every `didChange` event,
  regardless of whether the document has syntax errors.
- Priority: Must Have
- Dependencies: FR-002, FR-004.

---

## 7. Non-Functional Requirements

### 7.1 Performance Requirements

- **NFR-001:** Completion response latency must be at or below 200 ms for a
  500-line `.mlody` file, measured from `textDocument/didChange` receipt to
  `textDocument/completion` response dispatch, on developer hardware.
- **NFR-002:** Diff application (FR-002) must complete in O(lines changed) time,
  not O(total document length), for typical single-character edits.
- **NFR-003:** Tree-sitter incremental re-parse (FR-004) must be used on all
  `didChange` events where a previous tree exists, to avoid the cost of a full
  re-parse on every keystroke.

### 7.2 Correctness Requirements

- **NFR-004:** After any sequence of incremental edits, the full text maintained
  in `DocumentCache` must be byte-for-byte identical to the text that a
  `TextDocumentSyncKind.Full` client would have sent for the same final buffer
  state.
- **NFR-005:** Symbol extraction (FR-005) must never include a name from an
  incomplete or error-bearing assignment. In particular, `MY_MODEL = struct(`
  (unclosed call) must NOT contribute `MY_MODEL` to completions.
- **NFR-006:** All existing LSP features (completion, definition, hover,
  diagnostics, semantic tokens) must produce results identical to their
  pre-change behaviour for any fully-saved document state.

### 7.3 Code Quality

- **NFR-007:** All new and modified code must pass `basedpyright` strict mode
  with no suppression comments added beyond those already present in the
  codebase.
- **NFR-008:** All new and modified code must be formatted with `ruff`.
- **NFR-009:** New logic must be covered by `o_py_test` pytest tests; in
  particular:
  - Diff application correctness (single edit, multiple edits, full-text
    fallback).
  - Symbol extraction: completed assignment, completed `def`, incomplete
    assignment (error node), nested assignment (not extracted), `_`-prefixed
    name (not extracted).
  - Completion merge: evaluator-only, tree-only (evaluator None), both sources,
    deduplication.

### 7.4 Maintainability

- **NFR-010:** Diff application logic must be implemented as a pure, standalone
  function (no server state) so it is independently testable.
- **NFR-011:** Symbol extraction must be implemented as a pure function over
  `tree_sitter.Tree` with no side effects, co-located with or near
  `DocumentCache` in `parser.py`.

---

## 8. Data Requirements

### 8.1 Data Entities

**DocumentCache entry (updated):**

| Field     | Type               | Description                                    |
| --------- | ------------------ | ---------------------------------------------- |
| `uri`     | `str`              | LSP document URI, cache key                    |
| `version` | `int`              | LSP document version counter                   |
| `text`    | `str`              | Full document text after last diff application |
| `tree`    | `tree_sitter.Tree` | Parse tree corresponding to `text`             |

**SymbolName (extracted):**

| Field  | Type  | Description                              |
| ------ | ----- | ---------------------------------------- |
| `name` | `str` | Identifier text of the top-level binding |

### 8.2 Data Flow

```
textDocument/didChange
  │
  ├─ content_changes: list[TextDocumentContentChangePartial]
  │
  ▼
apply_incremental_changes(old_text, changes) → new_text   [FR-002]
  │
  ▼
DocumentCache.update(uri, version, new_text)
  │  reads old_tree from cache, calls parser.parse(bytes, old_tree=old_tree)
  │
  ▼
(version, new_text, new_tree) stored in cache              [FR-003, FR-004]
  │
  ├─► get_parse_diagnostics(new_tree) → diagnostics        [FR-007]
  │
  └─► (on completion request)
        extract_top_level_symbols(new_tree) → names        [FR-005]
        _general_completions(evaluator, file, tree)        [FR-006]
          = evaluator globals ∪ extracted names
```

---

## 9. Integration Requirements

### 9.1 LSP Protocol

| Aspect                            | Detail                                                      |
| --------------------------------- | ----------------------------------------------------------- |
| Sync kind advertised              | `TextDocumentSyncKind.Incremental`                          |
| Change event type (with range)    | `TextDocumentContentChangePartial` (lsprotocol)             |
| Change event type (full fallback) | `TextDocumentContentChangeWholeDocument` (lsprotocol)       |
| Client tested against             | Eglot (Emacs), which advertises `dynamicRegistration: true` |

### 9.2 tree-sitter API

| Call                 | Signature                            | Notes                  |
| -------------------- | ------------------------------------ | ---------------------- |
| First parse          | `parser.parse(bytes)`                | No old tree available  |
| Incremental re-parse | `parser.parse(bytes, old_tree=tree)` | `old_tree` is advisory |

---

## 10. Security & Compliance Requirements

No new attack surface is introduced. The diff application function operates on
in-memory strings and does not touch the filesystem. Existing security
properties of the server (no stdout writes, sandbox isolation) are unchanged.

---

## 11. Testing & Quality Assurance Requirements

### 11.1 Test Scope

| Area                        | Test file            | Key scenarios                                                                                                                                                                                |
| --------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Diff application            | `parser_test.py`     | Single insertion, single deletion, multi-edit sequence, full-text fallback (no range), empty changes list                                                                                    |
| Symbol extraction           | `parser_test.py`     | Completed assignment extracted; `def` extracted; incomplete assignment (ERROR node) not extracted; nested assignment not extracted; `_`-prefixed name not extracted; empty file returns `[]` |
| Completion merge            | `completion_test.py` | Evaluator-only (no unsaved); tree-only (`evaluator=None`); both sources with deduplication; ERROR node symbol absent from results                                                            |
| Incremental sync round-trip | `server_test.py`     | `didChange` with partial edits followed by `completion` returns updated symbol list                                                                                                          |

### 11.2 Acceptance Criteria Summary

1. `bazel test //mlody/lsp/...` passes with zero failures after implementation.
2. A test explicitly asserts that `MY_MODEL = struct(name="bert")` (complete
   assignment, unsaved) contributes `MY_MODEL` to completions.
3. A test explicitly asserts that `MY_MODEL = struct(` (incomplete assignment,
   parse error) does NOT contribute `MY_MODEL` to completions.
4. A test explicitly asserts that after a sequence of incremental edits, the
   document text in `DocumentCache` equals the expected final string.

---

## 12. Open Questions & Action Items

| ID     | Question/Action                                                                                                                                                                                                                                     | Owner       | Status |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ------ |
| OQ-001 | Confirm that Eglot sends changes in a single `content_changes` list entry per keystroke (not batched). If batched, the ordered application in FR-002 already handles it correctly, but a test with multiple simultaneous changes would be valuable. | Implementer | Open   |
| OQ-002 | Determine whether `tree_sitter.Parser.parse()` is safe to call concurrently (unlikely to be an issue given pygls single-thread dispatch, but worth confirming).                                                                                     | Implementer | Open   |
| OQ-003 | The `_general_completions` signature will need `tree` added as a parameter. Confirm this does not break any existing callers outside the test suite.                                                                                                | Implementer | Open   |

---

## 13. Revision History

| Version | Date       | Author                  | Changes       |
| ------- | ---------- | ----------------------- | ------------- |
| 1.0     | 2026-02-23 | Requirements Analyst AI | Initial draft |

---

## Appendix A: Glossary

- **Incremental sync** — LSP `TextDocumentSyncKind.Incremental`: the client
  sends only the changed ranges of the document on each edit, not the full text.
- **Diff application** — The process of applying a list of
  `TextDocumentContentChangePartial` edits to a stored full-text buffer to
  reconstruct the new full text.
- **Incremental re-parse** — tree-sitter's ability to reuse unchanged subtrees
  from a previous parse tree when re-parsing a slightly modified document,
  enabled by passing `old_tree` to `parser.parse()`.
- **Top-level symbol extraction** — Walking the direct children of the
  tree-sitter `module` root node to collect identifier names from completed
  assignments and `def` statements.
- **Same-file unsaved symbols** — Symbol names defined in the current file's
  in-memory buffer that have not yet been written to disk and therefore are
  absent from the starlarkish evaluator's `_module_globals`.
- **ERROR node** — A tree-sitter node of type `"ERROR"` produced by the parser's
  error-recovery mechanism when a construct cannot be parsed according to the
  grammar.

## Appendix B: References

- `mlody/lsp/CLAUDE.md` — Key library findings for tree-sitter, lsprotocol,
  pygls
- `mlody/lsp/FUTURE.md` — Deferred features and known limitations
- `mlody/SPEC.md` — MVP technical specification
- LSP Specification §3.15 (Text Document Synchronisation)
- tree-sitter Python bindings: `Parser.parse(source, old_tree=None)`

---

**End of Requirements Document**
