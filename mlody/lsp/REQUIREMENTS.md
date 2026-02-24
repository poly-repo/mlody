# Requirements Document: Starlark Tree-Sitter Parsing for mlody LSP

**Version:** 1.0
**Date:** 2026-02-23
**Prepared by:** Requirements Analyst AI
**Status:** Draft
**Traceable to:** Issue #388 — Add Starlark parsing to mlody language server

---

## 1. Executive Summary

The mlody Language Server Protocol (LSP) server currently parses `.mlody` files using ad-hoc regular expressions applied line-by-line. This approach is fundamentally limited: it cannot reason about document structure, fails on constructs that span multiple lines, and provides no foundation for advanced IDE capabilities such as diagnostics, hover, or semantic tokens. This document specifies the requirements for replacing and augmenting that approach by integrating the Python `tree-sitter` library with the `tree-sitter-starlark` grammar, introducing a central `parser.py` module that maintains a per-document parse-tree cache consumed by all current and future LSP feature handlers.

The expected outcome is a structurally sound LSP implementation that surfaces parse errors as live editor diagnostics, provides hover information for symbols, delivers semantic token data for spec-complete editor support, and correctly handles multi-line Starlark constructs — while preserving the existing semantic layer (the `starlarkish` evaluator) for symbol resolution.

---

## 2. Project Scope

### 2.1 In Scope

- A new `mlody/lsp/parser.py` module: a stateful document-cache wrapping the Python `tree-sitter` library with the `tree-sitter-starlark` grammar
- Refactoring `completion.py` and `definition.py` to consume `parser.py` for structural context detection, fixing the multi-line `load()` limitation
- A new `mlody/lsp/diagnostics.py` module: publish tree-sitter parse errors as LSP diagnostics on `textDocument/didOpen` and `textDocument/didChange`
- A new hover handler in `server.py` consuming `parser.py` and the starlarkish evaluator to show evaluated values or node type
- A new semantic tokens handler in `server.py` consuming `parser.py` to provide token classification data
- Integration of the `tree-sitter` Python package as a project dependency (added to `pyproject.toml`, locks regenerated via `o-repin`)
- Bazel integration: `//mlody/lsp:lsp_lib` updated to declare the new source files and the `@pip//tree_sitter` dependency
- Unit tests for all new and modified modules using `o_py_test`

### 2.2 Out of Scope

- Replacing the `starlarkish` evaluator for semantic understanding — tree-sitter provides syntactic structure only; symbol values continue to come from the evaluator
- Incremental (byte-range) tree-sitter re-parsing using `tree_sitter.Parser.edit()` — full re-parse on document change is acceptable for MVP
- A standalone `diagnostics.py` binary or CLI surface — diagnostics are LSP-only
- Custom grammar modifications to `tree-sitter-starlark` for `.mlody`-specific syntax extensions
- Rename (`textDocument/rename`) and references (`textDocument/references`) capabilities — these are deferred to future issues
- A `tree-sitter` grammar compilation step inside the Bazel build — the grammar is loaded at runtime via the `tree-sitter` Python package's language-binding mechanism
- Updating the Emacs `neo-mlody-mode.el` tree-sitter configuration — the Emacs side already has its own grammar (the `libtree-sitter-starlark.so` in `devex/editors/emacs/tree-sitter/`) and is unaffected by this change

### 2.3 Assumptions

- The `tree-sitter` Python package (PyPI: `tree-sitter`) and the `tree-sitter-starlark` language binding (PyPI: `tree-sitter-starlark`) are available on PyPI and can be added to `pyproject.toml` without version pinning
- The `tree-sitter-starlark` grammar parses `.mlody` files correctly because `.mlody` is a strict subset of Starlark syntax with no custom grammar extensions
- The existing `starlarkish` evaluator's `_module_globals` dict remains the authoritative source for evaluated symbol values; tree-sitter does not replace it
- The LSP client (Emacs Eglot, and any other compliant client) supports `textDocument/publishDiagnostics`, `textDocument/hover`, and `textDocument/semanticTokens/full`
- The server's `TextDocumentSyncKind` can be upgraded to `FULL` (send entire document text on change) without breaking any existing client integration

### 2.4 Constraints

- Must use `o_py_library`, `o_py_binary`, `o_py_test` from `//build/bzl:python.bzl` — no raw `py_*` rules
- All Python code must pass `basedpyright` strict mode with full type annotations
- All Python code must be formatted with `ruff`
- Dependencies managed via `uv pip compile` — do not pin versions with `==` in `pyproject.toml`; run `o-repin` after adding dependencies
- The LSP server communicates over stdio; no writes to stdout are permitted from any module (including tree-sitter) outside of the pygls JSON-RPC framing

---

## 3. Stakeholders

| Role | Description | Primary Interest in This Change |
|------|-------------|--------------------------------|
| Pipeline Builder | Writes `.mlody` pipeline definitions | Live syntax error squiggles, hover showing values, better completions on multi-line files |
| Platform Engineer | Maintains the mlody framework | Clean architecture, testable parser module, foundation for future LSP capabilities |
| Editor Integration Engineer | Configures editor support (NEO / Eglot) | Semantic tokens for editors that cannot do their own tree-sitter highlighting |

---

## 4. Problem Statement

### 4.1 Limitations of the Current Regex Approach

The current `completion.py` and `definition.py` modules parse `.mlody` source text using a set of regular expressions applied to a single line of text at a time (the line under the cursor). This design has the following documented and structural deficiencies:

**Structural deficiency 1 — No multi-line awareness.**
`_LOAD_RE` in `completion.py` and `_LOAD_PATH_RE` in `definition.py` match only when the entire `load(...)` call fits on one line. A `load()` whose path string and symbol list span multiple lines is silently ignored by both providers:

```starlark
# This multi-line load() is invisible to the current LSP
load(
    "//mlody/core/builtins.mlody",
    "root",
)
```

**Structural deficiency 2 — No syntax error feedback.**
The server has no mechanism to detect or report Starlark syntax errors in `.mlody` files. A user with a typo or malformed expression receives no editor diagnostic — the file silently fails to evaluate and completions degrade without explanation.

**Structural deficiency 3 — No parse-tree foundation for hover or semantic tokens.**
Hover requires knowing the syntactic role of the token under the cursor (is it an identifier, a string literal, a keyword?). Semantic tokens require classifying every token in the document by type and modifier. Neither capability can be implemented reliably from single-line regex matching.

**Structural deficiency 4 — Fragile context detection.**
`_detect_context()` uses three mutually exclusive regex patterns applied to the current line. Adding new completion contexts (e.g., inside a function call argument, inside a dict literal) requires adding more regex branches, each of which can interfere with existing ones. A tree-sitter query over the AST node containing the cursor is robust where regex is fragile.

---

## 5. Goals

- **G-001:** Provide a central, reusable parse-tree module (`parser.py`) that any LSP handler can query without re-parsing the document
- **G-002:** Fix multi-line `load()` handling in completion and go-to-definition using tree-sitter queries instead of single-line regex
- **G-003:** Surface Starlark parse errors as live LSP diagnostics (squiggles in the editor) updated on every document change
- **G-004:** Provide hover information combining syntactic context (node type from tree-sitter) with semantic context (evaluated value from the starlarkish evaluator)
- **G-005:** Deliver semantic token data via the LSP semantic tokens protocol for spec-completeness and editor portability
- **G-006:** Establish `parser.py` as the documented integration point for all future parse-tree-dependent LSP capabilities (rename, references, diagnostics enrichment, etc.)

---

## 6. Non-Goals

- Replacing the `starlarkish` evaluator — tree-sitter is a syntactic tool only; `Evaluator._module_globals` remains the semantic source
- Incremental parse-tree editing (byte-range updates) — full re-parse on change is acceptable
- Modifying the `tree-sitter-starlark` grammar itself
- Providing tree-sitter support in the CLI (`mlody show`, `mlody shell`) — this change is LSP-only
- Updating the Emacs tree-sitter grammar configuration in `neo-mlody-mode.el`
- Implementing rename or references LSP capabilities (separate future issues)
- Supporting editors over transports other than stdio

---

## 7. Functional Requirements

### 7.1 Parser Module

---

**FR-001: Document parse-tree cache**

- **Description:** `parser.py` shall maintain an in-process cache mapping document URI to a `(version: int, tree: tree_sitter.Tree)` tuple. A cache entry is created or updated when a document is opened or changed; it is evicted when a document is closed.
- **Rationale:** Avoids re-parsing the same document version on every LSP request. Multiple handlers (completion, definition, hover, diagnostics, semantic tokens) all require the parse tree for the same request cycle.
- **Interface:**
  ```python
  class DocumentCache:
      def update(self, uri: str, version: int, text: str) -> tree_sitter.Tree: ...
      def get(self, uri: str) -> tree_sitter.Tree | None: ...
      def remove(self, uri: str) -> None: ...
  ```
- **Behavior:** If `update()` is called with a `version` equal to the cached version for that URI, the existing tree is returned without re-parsing. If `version` is greater, the document is re-parsed and the cache entry replaced. Version numbers are those provided by the LSP client in `TextDocumentItem.version` and `VersionedTextDocumentIdentifier.version`.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a document is opened with version 1, when `update(uri, 1, text)` is called a second time, then no re-parse occurs (the same tree object is returned)
  - Given a document changes to version 2, when `update(uri, 2, new_text)` is called, then a fresh parse is performed and the new tree returned
  - Given a document is closed, when `remove(uri)` is called, then the cache entry is deleted and subsequent `get(uri)` returns `None`
  - Given a document text with a syntax error, when `update()` is called, then a tree is still returned (tree-sitter produces partial trees on error) and `tree.root_node.has_error` is `True`

---

**FR-002: Grammar loading**

- **Description:** `parser.py` shall load the `tree-sitter-starlark` grammar at module import time using the `tree_sitter_starlark` Python package. The `tree_sitter.Language` object and `tree_sitter.Parser` instance shall be module-level singletons.
- **Rationale:** Grammar loading is expensive; it must not happen per-request or per-document. A module-level singleton initialised at import time is the standard pattern.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the `tree_sitter_starlark` package is installed, when `parser.py` is imported, then `STARLARK_LANGUAGE` is a valid `tree_sitter.Language` instance
  - Given the module is imported multiple times (e.g., in tests), then grammar loading occurs only once (module-level initialisation)
  - Given `parser.py` is imported and a call is made to `DocumentCache.update()`, then the returned tree's root node language matches the starlark language

---

**FR-003: Tree-sitter node query helpers**

- **Description:** `parser.py` shall expose helper functions that LSP handlers use to query the parse tree without duplicating tree traversal logic. At minimum:
  - `node_at_position(tree, line, character) -> tree_sitter.Node`: return the deepest node whose range contains the given LSP position
  - `find_ancestor(node, type_name) -> tree_sitter.Node | None`: walk parent links until a node of the named type is found, or return `None`
  - `get_load_statements(tree) -> list[LoadStatement]`: return a structured list of all `load()` calls in the document regardless of line count, each with the load path string and the list of imported symbol names and their positions
- **Rationale:** These are the primitives that completion, definition, hover, and diagnostics all need. Centralising them ensures consistent behaviour and avoids duplicated tree traversal in each provider.
- **Data type:**
  ```python
  @dataclass(frozen=True)
  class LoadStatement:
      path: str                          # e.g. "//mlody/core/builtins.mlody"
      path_node: tree_sitter.Node        # the string node for the path
      symbols: list[ImportedSymbol]

  @dataclass(frozen=True)
  class ImportedSymbol:
      name: str
      node: tree_sitter.Node             # the string node for this symbol
  ```
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a document with `load("//a.mlody", "X")` on a single line, when `get_load_statements()` is called, then one `LoadStatement` is returned with `path == "//a.mlody"` and one symbol `name == "X"`
  - Given a document with a multi-line `load()` call (path on one line, symbols on subsequent lines), when `get_load_statements()` is called, then the same `LoadStatement` structure is returned as for the single-line form
  - Given an LSP position inside the word `MY_VAR`, when `node_at_position()` is called, then the returned node is an `identifier` node spanning that word
  - Given a node of type `identifier` inside an `assignment`, when `find_ancestor(node, "assignment")` is called, then the `assignment` node is returned

---

### 7.2 Completion Provider Refactoring

---

**FR-004: Replace single-line context detection with tree-sitter query**

- **Description:** `completion.py` shall replace the `_detect_context()` regex function with a tree-sitter query against the parse tree to determine the cursor's syntactic context. Context detection shall use `node_at_position()` and `find_ancestor()` from `parser.py`.
- **Contexts to detect:**
  - `load_path` — cursor is inside the path string argument of a `load()` call
  - `load_symbol` — cursor is inside a symbol string argument of a `load()` call (after the path)
  - `builtins_member` — cursor follows `builtins.` (attribute access on the `builtins` identifier)
  - `general` — all other positions
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the cursor is on the path string of a multi-line `load()` call (path on a different line from the opening paren), when `get_completions()` is called, then `load_path` context is detected and file-path completions are returned
  - Given the cursor is on a symbol string in a multi-line `load()`, when `get_completions()` is called, then `load_symbol` context is detected (even if the path is on a prior line)
  - Given the cursor is after `builtins.` anywhere on any line, when `get_completions()` is called, then `builtins_member` context is detected
  - Given the cursor is on a blank line or inside a general expression, when `get_completions()` is called, then `general` context is detected and safe-builtins + loaded-symbols completions are returned
  - All existing tests in `completion_test.py` continue to pass

---

**FR-005: Multi-line load() path completions**

- **Description:** `completion.py` shall correctly provide file-path completions when the cursor is inside the path string of a `load()` call that spans multiple lines.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a document containing:
    ```starlark
    load(
        "//mlody/
    ```
    and the cursor is at the end of the second line, when `get_completions()` is called, then directory and `.mlody` file completions rooted at `monorepo_root/mlody/` are returned

---

### 7.3 Definition Provider Refactoring

---

**FR-006: Replace single-line load extraction with tree-sitter query**

- **Description:** `definition.py` shall replace `_extract_load_string()` and the `re.finditer()` loop in `get_definition()` with calls to `get_load_statements()` from `parser.py`. Definition resolution for `load()` paths and imported symbols shall work when the `load()` call is split across multiple lines.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a multi-line `load()` call with the cursor on the path string node, when `get_definition()` is called, then the correct file location is returned
  - Given a multi-line `load()` call with the cursor on a symbol string node (e.g., `"MY_VAR"` on its own line), when `get_definition()` is called, then the definition line for `MY_VAR` in the source file is returned
  - All existing tests in `definition_test.py` continue to pass

---

### 7.4 Diagnostics

---

**FR-007: Publish parse-error diagnostics on document open**

- **Description:** `server.py` shall register a `textDocument/didOpen` handler. When a `.mlody` document is opened, the server shall parse it via `parser.py`, extract all error nodes from the parse tree, and publish them as LSP diagnostics via `textDocument/publishDiagnostics`.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a `.mlody` document with a syntax error is opened, when the editor opens it, then the editor receives a `publishDiagnostics` notification with at least one diagnostic whose `severity` is `DiagnosticSeverity.Error`
  - Given the diagnostic, then its `range` corresponds to the position of the error node in the document
  - Given the diagnostic, then its `message` is a human-readable description (e.g., `"Syntax error"` or the tree-sitter node type that failed to parse)
  - Given the diagnostic, then its `source` is `"mlody-lsp"`
  - Given a syntactically valid `.mlody` document is opened, then a `publishDiagnostics` notification with an empty `diagnostics` list is sent (clearing any prior errors)

---

**FR-008: Publish parse-error diagnostics on document change**

- **Description:** `server.py` shall register a `textDocument/didChange` handler. When a `.mlody` document changes (the client sends the full updated text), the server shall update the `DocumentCache`, extract error nodes, and publish diagnostics identically to FR-007.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a previously valid document where the user introduces a syntax error by typing, when `didChange` is received, then a `publishDiagnostics` notification with the new error is sent within the same request cycle
  - Given a document with a prior error where the user fixes the syntax, when `didChange` is received, then a `publishDiagnostics` notification with an empty `diagnostics` list is sent
  - Given a document with multiple distinct error nodes, then all error node positions are reported as separate diagnostics in a single `publishDiagnostics` call

---

**FR-009: Diagnostics module**

- **Description:** `mlody/lsp/diagnostics.py` shall expose two pure functions consumed by `server.py`: one for tree-sitter parse errors and one for evaluator runtime errors. `server.py` merges both lists into a single `publishDiagnostics` call per document.
- **Interface:**
  ```python
  def get_parse_diagnostics(tree: tree_sitter.Tree) -> list[lsprotocol.types.Diagnostic]: ...
  def get_eval_diagnostics(exc: Exception, uri: str) -> list[lsprotocol.types.Diagnostic]: ...
  ```
- **Behavior of `get_parse_diagnostics`:** Walks the tree-sitter parse tree depth-first, collects all nodes where `node.is_error` is `True` or `node.is_missing` is `True`, and converts each to an `lsprotocol.types.Diagnostic` with:
  - `range`: node's start/end position (tree-sitter `(row, column)` → LSP `Position(line, character)`)
  - `severity`: `DiagnosticSeverity.Error`
  - `message`: `f"Syntax error: unexpected '{node.type}'"` for error nodes; `f"Syntax error: missing '{node.type}'"` for missing nodes
  - `source`: `"mlody-lsp"`
- **Behavior of `get_eval_diagnostics`:** Converts a Python exception raised by the starlarkish evaluator into an `lsprotocol.types.Diagnostic`:
  - `SyntaxError`: use `exc.lineno` and `exc.offset` for the range (1-indexed → 0-indexed); message is `f"SyntaxError: {exc.msg}"`
  - All other exceptions (`NameError`, `ImportError`, `Exception`): inspect `exc.__traceback__` for the innermost frame in the document's file; if found, use that frame's `tb_lineno`; if not, fall back to line 0 (document-level). Message is `f"{type(exc).__name__}: {exc}"`
  - `severity`: `DiagnosticSeverity.Error`
  - `source`: `"mlody-lsp"`
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a parse tree with no error nodes, when `get_parse_diagnostics()` is called, then an empty list is returned
  - Given a parse tree with one `ERROR` node, when `get_parse_diagnostics()` is called, then a list with exactly one `Diagnostic` is returned with correct range and severity
  - Given a parse tree with a `MISSING` node, when `get_parse_diagnostics()` is called, then a diagnostic with message containing `"missing"` is returned
  - Given a `SyntaxError` with `lineno=3` and `offset=5`, when `get_eval_diagnostics()` is called, then the diagnostic range starts at `Position(line=2, character=4)`
  - Given a `NameError` with a traceback pointing to line 7 of the document, when `get_eval_diagnostics()` is called, then the diagnostic range starts at `Position(line=6, character=0)`
  - Given an exception whose traceback does not reference the document file, when `get_eval_diagnostics()` is called, then the diagnostic range is `Position(line=0, character=0)` to `Position(line=0, character=0)`

---

**FR-009a: Evaluator error capture in server.py**

- **Description:** `server.py` shall catch exceptions from `Workspace.load()` (on `INITIALIZED` and `didChangeWatchedFiles`) and store the exception alongside `_evaluator`. When publishing diagnostics for an open document, if an evaluator exception was captured for a file matching the document URI, `get_eval_diagnostics()` shall be called and its results merged with the tree-sitter parse diagnostics.
- **State additions to `server.py`:**
  ```python
  _eval_error: Exception | None = None  # set when Workspace.load() raises
  ```
- **Behavior:** The merged diagnostics list (parse errors + eval errors) is published in a single `publishDiagnostics` notification. If neither source produces errors, an empty list is published (clearing prior diagnostics).
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given `Workspace.load()` raises a `NameError`, when `didOpen` fires for the affected `.mlody` file, then a `publishDiagnostics` notification is sent with a diagnostic whose message starts with `"NameError:"`
  - Given `Workspace.load()` raises a `SyntaxError` at line 5, when `didOpen` fires, then the diagnostic range includes line 4 (0-indexed)
  - Given a file with both a tree-sitter parse error and an evaluator `NameError`, when diagnostics are published, then both appear in the same `publishDiagnostics` notification
  - Given the workspace reloads successfully after a prior failure, when `didOpen` fires, then `_eval_error` is `None` and no evaluator diagnostics are published

---

### 7.5 Hover

---

**FR-010: Hover handler registration**

- **Description:** `server.py` shall register a `textDocument/hover` handler using `@server.feature(types.TEXT_DOCUMENT_HOVER)`.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the LSP client sends a `textDocument/hover` request, then the server responds (not with an error or null from unregistered capability)
  - Given the cursor is not on a navigable token, then the handler returns `None` (no hover content)

---

**FR-011: Hover content — evaluated value**

- **Description:** When the cursor is on an `identifier` node whose name is present in the current file's evaluator module globals (`evaluator._module_globals[current_file]`), the hover response shall include the symbol's Python `repr()` value.
- **Format:** Markdown, rendered as:
  ```
  **identifier**
  Value: `<repr of value>`
  ```
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the cursor is on `MY_CONST` and `MY_CONST = "hello world"` is in the evaluator globals, when hover is invoked, then the hover content contains `Value: \`'hello world'\``
  - Given the cursor is on `MY_STRUCT` and its value is a `Struct` instance, when hover is invoked, then the hover content contains `Value:` followed by the `repr()` of the struct
  - Given the evaluator is `None` (workspace failed to load), when hover is invoked on any identifier, then the server returns `None` (no crash)

---

**FR-012: Hover content — load() path string**

- **Description:** When the cursor is on the path string node of a `load()` call (e.g., `"//mlody/core/builtins.mlody"`), the hover response shall show the resolved absolute filesystem path using `_resolve_load_path()` from `definition.py`.
- **Format:** Markdown:
  ```
  **load path**
  `<absolute/filesystem/path.mlody>`
  ```
  If the path does not resolve to an existing file (e.g., the string is incomplete or the file is missing), the hover shall show the unresolved path string with a note that the file was not found:
  ```
  **load path**
  `//mlody/missing.mlody` *(file not found)*
  ```
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the cursor is on `"//mlody/core/builtins.mlody"` in a `load()` call and the file exists on disk, when hover is invoked, then the hover content contains the absolute path to that file
  - Given the cursor is on a `load()` path string that does not resolve to an existing file, when hover is invoked, then the hover content contains the raw load string and `*(file not found)*`
  - Given the cursor is on a symbol string argument of a `load()` call (not the path), then FR-011/FR-013 apply (identifier value or node type), not this requirement

---

**FR-013: Hover content — fallback to node type**

- **Description:** When the cursor is on a token that is not an identifier in the evaluator globals and not a `load()` path string (e.g., a keyword, builtin name, or an identifier not yet evaluated), the hover response shall show the tree-sitter node type as a fallback.
- **Format:** Markdown:
  ```
  **<node_type>**
  ```
- **Priority:** Should Have
- **Acceptance Criteria:**
  - Given the cursor is on the keyword `def`, when hover is invoked, then the hover content contains `**keyword**`
  - Given the cursor is on a builtin name like `struct` (which is in `SAFE_BUILTINS` but not in `_module_globals`), when hover is invoked, then the fallback node-type hover is shown
  - Given the cursor is on a plain string literal that is not a `load()` path, when hover is invoked, then the hover content contains `**string**`

---

### 7.6 Semantic Tokens

---

**FR-013: Semantic tokens provider registration**

- **Description:** `server.py` shall register a `textDocument/semanticTokens/full` handler using `@server.feature(types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL, ...)` with a `SemanticTokensLegend` declaring the supported token types and modifiers.
- **Token types to declare** (subset of LSP standard types):
  - `keyword`
  - `string`
  - `comment`
  - `function` (function definitions)
  - `variable` (identifiers in assignment targets)
  - `parameter` (function parameters)
  - `number` (integer and float literals)
- **Token modifiers to declare:**
  - `definition` (the defining occurrence of a name)
  - `readonly` (module-level assignments, which are effectively constants in Starlark)
- **Priority:** Could Have
- **Acceptance Criteria:**
  - Given the client sends `textDocument/semanticTokens/full`, then the server responds with a valid `SemanticTokens` object (encoded integer array per the LSP spec) — not `None` and not an error
  - Given a `.mlody` document with `def my_func():`, when semantic tokens are requested, then the token for `my_func` has type `function` and modifier `definition`
  - Given a `.mlody` document with `MY_CONST = 42`, when semantic tokens are requested, then `MY_CONST` has type `variable` and modifier `definition | readonly`, and `42` has type `number`
  - Given a `.mlody` document with a comment `# comment`, when semantic tokens are requested, then the comment token has type `comment`

---

**FR-014: Semantic tokens encoding**

- **Description:** The semantic tokens response shall be encoded as a flat integer array per the LSP 3.16 spec: each token is represented by 5 integers `[deltaLine, deltaStartChar, length, tokenType, tokenModifiers]` relative to the previous token.
- **Priority:** Could Have
- **Acceptance Criteria:**
  - Given a document with two tokens on separate lines, when the encoded array is decoded, then `deltaLine` for the second token equals the line difference between the tokens
  - Given a document with two tokens on the same line, when decoded, then `deltaLine` for the second token is `0` and `deltaStartChar` is the character offset difference

---

### 7.7 Document Sync

---

**FR-015: Upgrade TextDocumentSyncKind to FULL**

- **Description:** The `LanguageServer` capability advertisement shall declare `TextDocumentSyncKind.FULL` so the client sends the complete document text on every `didChange` notification (not just incremental deltas). This is required for FR-008 because tree-sitter re-parse requires the full text.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given the server initialises, when the client reads the `initialize` response, then `capabilities.textDocumentSync.change` equals `TextDocumentSyncKind.Full` (value `1`)
  - Given a `didChange` notification is received, then `params.content_changes[0].text` contains the complete document text

---

**FR-016: didClose handler — cache eviction**

- **Description:** `server.py` shall register a `textDocument/didClose` handler that calls `DocumentCache.remove(uri)` to evict the closed document's parse tree from the cache.
- **Priority:** Must Have
- **Acceptance Criteria:**
  - Given a document is open in the cache, when `didClose` is received, then `DocumentCache.get(uri)` returns `None`
  - Given multiple documents are open, when one is closed, then the other documents remain in the cache

---

## 8. Non-Functional Requirements

**NFR-001: Type correctness**
All new and modified Python modules must pass `basedpyright` in strict mode with no type errors or `# type: ignore` suppressions except where tree-sitter's own type stubs are incomplete (which must be documented inline with a comment explaining the suppression).

**NFR-002: Code formatting**
All new and modified Python modules must be formatted with `ruff` with no formatting or lint violations.

**NFR-003: Test coverage**
Each new module (`parser.py`, `diagnostics.py`) must have a corresponding `_test.py` file with unit tests covering the public API. Tests must use `o_py_test` (which auto-injects pytest and debugpy). Test files must use `pyfakefs` or `InMemoryFS` from `//common/python/starlarkish/evaluator:testing` where filesystem access is needed.

**NFR-004: LSP response latency**
The `DocumentCache` must ensure that completion, hover, and definition handlers do not re-parse the document on every request. Parse operations shall occur only on `didOpen` and `didChange` events. Completion and hover responses for a cached document must return within 100ms for typical `.mlody` files (under 500 lines).

**NFR-005: No stdout contamination**
The tree-sitter library must not write to stdout. The server communicates exclusively over stdio via pygls JSON-RPC framing. Any debug or warning output from the tree-sitter library must be suppressed or redirected to the LSP log handler (`LSPLogHandler`).

**NFR-006: Graceful degradation**
If the `tree_sitter_starlark` package fails to load at import time (e.g., missing native library), `parser.py` must raise a clear `ImportError` with an actionable message. The server must not crash silently; it should fail fast at startup rather than producing undefined behaviour at request time.

**NFR-007: Backward compatibility**
The existing `completion.py` and `definition.py` public APIs (`get_completions`, `get_definition`) must preserve their existing signatures. Internal implementation changes are permitted; callers in `server.py` must not require modification to their call sites.

**NFR-008: Bazel hermeticity**
The `tree-sitter` and `tree-sitter-starlark` packages must be declared as `@pip//tree_sitter` and `@pip//tree_sitter_starlark` in the relevant `BUILD.bazel` `deps` lists. No dynamic pip installs or sys.path manipulation at runtime.

---

## 9. Architecture

### 9.1 Module Dependency Graph

```
server.py
  │
  ├── parser.py           ← NEW: tree-sitter wrapper + DocumentCache
  │     (imported by all feature handlers)
  │
  ├── diagnostics.py      ← NEW: get_diagnostics(tree) -> list[Diagnostic]
  │     (pure function, no server state)
  │
  ├── completion.py       ← MODIFIED: uses parser.py for context detection
  │
  ├── definition.py       ← MODIFIED: uses parser.py for load() extraction
  │
  └── log_handler.py      ← UNCHANGED
```

### 9.2 `parser.py` Internal Structure

```python
# Module-level singletons — initialised at import time
import tree_sitter_starlark
from tree_sitter import Language, Parser

STARLARK_LANGUAGE: Language = Language(tree_sitter_starlark.language())
_parser: Parser = Parser(STARLARK_LANGUAGE)

# Public API
class DocumentCache:
    _cache: dict[str, tuple[int, tree_sitter.Tree]]

    def update(self, uri: str, version: int, text: str) -> tree_sitter.Tree: ...
    def get(self, uri: str) -> tree_sitter.Tree | None: ...
    def remove(self, uri: str) -> None: ...

@dataclass(frozen=True)
class ImportedSymbol:
    name: str
    node: tree_sitter.Node

@dataclass(frozen=True)
class LoadStatement:
    path: str
    path_node: tree_sitter.Node
    symbols: list[ImportedSymbol]

def node_at_position(tree: tree_sitter.Tree, line: int, character: int) -> tree_sitter.Node: ...
def find_ancestor(node: tree_sitter.Node, type_name: str) -> tree_sitter.Node | None: ...
def get_load_statements(tree: tree_sitter.Tree) -> list[LoadStatement]: ...
```

### 9.3 `server.py` Cache Lifecycle

```
didOpen(uri, version=1, text)
  → DocumentCache.update(uri, 1, text)         # parse and cache
  → diagnostics.get_diagnostics(tree)           # extract errors
  → server.publish_diagnostics(uri, diags)      # push to client

didChange(uri, version=2, text)
  → DocumentCache.update(uri, 2, text)          # re-parse (new version)
  → diagnostics.get_diagnostics(tree)
  → server.publish_diagnostics(uri, diags)

didClose(uri)
  → DocumentCache.remove(uri)                   # evict

completion(uri, position)
  → DocumentCache.get(uri)                      # cache hit — no re-parse
  → parser.node_at_position(tree, ...)
  → completion context detection
  → return CompletionList

hover(uri, position)
  → DocumentCache.get(uri)                      # cache hit
  → parser.node_at_position(tree, ...)
  → evaluator._module_globals lookup
  → return Hover

definition(uri, position)
  → DocumentCache.get(uri)                      # cache hit
  → parser.get_load_statements(tree)
  → resolve path or symbol
  → return Location

semanticTokens/full(uri)
  → DocumentCache.get(uri)                      # cache hit
  → walk tree, classify tokens
  → return SemanticTokens
```

### 9.4 Cache Miss Handling

A cache miss occurs when a handler is called for a URI that was never opened via `didOpen` (e.g., a client that does not send `didOpen` before `completion`). In this case, `DocumentCache.get(uri)` returns `None`. Handlers shall **eagerly parse** on a cache miss:

```python
tree = cache.get(uri)
if tree is None:
    doc = server.workspace.get_text_document(uri)
    tree = cache.update(uri, 0, doc.source)
```

This eliminates dual code paths. The old regex logic in `completion.py` and `definition.py` shall be removed entirely once refactoring is complete (no fallback preserved).

---

## 10. Dependencies

### 10.1 New Python Dependencies

| Package | PyPI Name | Purpose | Add to `pyproject.toml` |
|---------|-----------|---------|------------------------|
| `tree-sitter` | `tree-sitter` | Core tree-sitter Python bindings; provides `Language`, `Parser`, `Tree`, `Node` | Yes |
| `tree-sitter-starlark` | `tree-sitter-starlark` (v1.3.0) | Starlark language grammar as a Python package; provides `tree_sitter_starlark.language()`; imports as `import tree_sitter_starlark` | Yes |

After adding both to `pyproject.toml` `dependencies`, regenerate lock files with `o-repin`.

### 10.2 Bazel Dependency Declarations

The following `deps` entries must be added to the relevant `BUILD.bazel` targets:

```starlark
# mlody/lsp/BUILD.bazel — lsp_lib
deps = [
    ...existing deps...,
    "@pip//tree_sitter",
    "@pip//tree_sitter_starlark",
]
```

New test targets for `parser_test.py` and `diagnostics_test.py` must also declare these deps.

### 10.3 Existing Infrastructure (No Changes Required)

| Component | Location | Role |
|-----------|----------|------|
| `libtree-sitter-starlark.so` | `devex/editors/emacs/tree-sitter/` | Emacs-side grammar; unchanged by this feature |
| `neo-mlody-mode.el` | `devex/editors/emacs/extensions/extensions/neo/mlody/` | Emacs tree-sitter major mode; unchanged |
| `starlarkish` evaluator | `common/python/starlarkish/` | Semantic layer; unchanged |
| `pygls` | `@pip//pygls` | LSP server framework; unchanged |
| `lsprotocol` | `@pip//lsprotocol` | LSP type definitions; unchanged |

### 10.4 Grammar Source Reference

The `tree-sitter-starlark` Python package wraps the grammar from:
`https://github.com/tree-sitter-grammars/tree-sitter-starlark`

This is the same grammar source referenced in `neo-mlody-mode.el` line 16:
```elisp
(add-to-list 'treesit-language-source-alist
             '(starlark "https://github.com/tree-sitter-grammars/tree-sitter-starlark"))
```

Consistency between the Emacs and Python grammar sources is intentional.

---

## 11. Testing Requirements

### 11.1 New Test Files

| Test file | Module under test | Key scenarios |
|-----------|-------------------|---------------|
| `parser_test.py` | `parser.py` | Grammar loads; `DocumentCache` version gating; `node_at_position` on valid and error trees; `find_ancestor` traversal; `get_load_statements` single-line and multi-line |
| `diagnostics_test.py` | `diagnostics.py` | Empty diagnostics for valid tree; error diagnostic for tree with `ERROR` node; missing-node diagnostic; correct range conversion from tree-sitter `(row, col)` to LSP `Position`; `SyntaxError` → diagnostic with correct line/col; `NameError` → diagnostic with traceback-derived line; exception with no matching frame → line-0 fallback |

### 11.2 Modified Test Files

| Test file | Changes required |
|-----------|-----------------|
| `completion_test.py` | Add multi-line `load()` test cases for context detection and path completions; existing tests must continue passing |
| `definition_test.py` | Add multi-line `load()` test cases for path and symbol navigation; existing tests must continue passing |
| `server_test.py` | Add tests for `didOpen` / `didChange` triggering `publishDiagnostics`; add hover handler test |

### 11.3 Test Execution

```sh
bazel test //mlody/lsp/...            # All LSP tests
bazel test //mlody/lsp:parser_test    # New parser module only
bazel build --config=lint //mlody/... # Lint check
```

---

## 12. Implementation Phases

Given the dependency relationships, implementation should proceed in this order:

| Phase | Work | Depends on |
|-------|------|------------|
| **Phase 1** | Add `tree-sitter` and `tree-sitter-starlark` to `pyproject.toml`; run `o-repin`; update `BUILD.bazel` | — |
| **Phase 2** | Implement `parser.py` (`DocumentCache`, grammar loading, query helpers); write `parser_test.py` | Phase 1 |
| **Phase 3** | Implement `diagnostics.py` and `get_diagnostics()`; write `diagnostics_test.py` | Phase 2 |
| **Phase 4** | Register `didOpen`, `didChange`, `didClose` handlers in `server.py`; wire diagnostics publication | Phase 2, 3 |
| **Phase 5** | Refactor `completion.py` to use `parser.py` for context detection; update `completion_test.py` | Phase 2, 4 |
| **Phase 6** | Refactor `definition.py` to use `parser.py` for load extraction; update `definition_test.py` | Phase 2, 4 |
| **Phase 7** | Implement hover handler in `server.py`; write hover tests in `server_test.py` | Phase 2, 4 |
| **Phase 8** | Implement semantic tokens handler in `server.py`; write semantic token tests | Phase 2, 4 |

Phases 5 and 6 can proceed in parallel. Phases 7 and 8 can proceed in parallel after Phase 4.

---

## 13. Risks and Mitigations

| Risk ID | Description | Impact | Probability | Mitigation |
|---------|-------------|--------|-------------|------------|
| R-001 | `tree-sitter-starlark` PyPI package may not exist under that exact name or may lag the grammar repo | High | Medium | Verify package name and availability on PyPI before starting Phase 1; alternative is `tree-sitter` core with manual grammar compilation via `Language.build_library()` |
| R-002 | tree-sitter `Node` objects hold a reference to the `Tree`; if the tree is replaced in the cache while a handler holds a node reference, the node may become invalid | Medium | Low | Handlers must complete all tree-sitter node access before returning; do not store raw `Node` objects outside request scope |
| R-003 | `TextDocumentSyncKind.FULL` sends the entire document on every keystroke; for very large `.mlody` files this increases JSON-RPC payload size | Low | Low | Acceptable for typical `.mlody` file sizes (under 1000 lines); document the limitation; incremental sync is a future optimisation |
| R-004 | The existing `completion.py` and `definition.py` tests mock `evaluator._module_globals` directly; if the cache is introduced, tests that bypass `didOpen` will trigger an eager parse via `server.workspace.get_text_document()` | Medium | Medium | Update test setup to pre-populate the `DocumentCache` directly, or mock `server.workspace.get_text_document()` to return fixture text |
| R-005 | Semantic token encoding (5-integer delta encoding) is complex to implement correctly; bugs produce garbled highlighting silently | Low | Medium | Write dedicated round-trip tests that encode then decode the integer array and compare to expected token list |

---

## 14. Open Questions

| ID | Question | Owner | Target Date | Status |
|----|----------|-------|-------------|--------|
| OQ-001 | **Cache miss handling strategy:** Resolved. On a cache miss, handlers shall eagerly parse the document using `server.workspace.get_text_document(uri).source` and call `DocumentCache.update()` before proceeding. No dual regex/tree-sitter fallback paths. The old regex logic in `completion.py` and `definition.py` can be removed entirely once refactoring is complete. | [Resolved] | Before Phase 4 | **Closed** |
| OQ-002 | **Exact PyPI package name for the Starlark grammar:** Resolved. PyPI name is `tree-sitter-starlark` (hyphen); Python import is `import tree_sitter_starlark`; loading API is `tree_sitter_starlark.language()`. Current version: 1.3.0. Add `tree-sitter-starlark` to `pyproject.toml` without version pin. | [Resolved] | Phase 1 | **Closed** |
| OQ-003 | **Hover for `load()` path strings:** Resolved. Hovering over a `load()` path string shall show the resolved absolute filesystem path (using `_resolve_load_path()` from `definition.py`). If the file does not exist, show the raw load string with `*(file not found)*`. Captured in FR-012. | [Resolved] | Before Phase 7 | **Closed** |
| OQ-004 | **Diagnostics for evaluation errors:** Resolved. Evaluator exceptions from `Workspace.load()` shall be captured in `_eval_error` and published as LSP diagnostics alongside tree-sitter parse errors. `SyntaxError` uses `.lineno`/`.offset` for positioning; other exceptions use traceback inspection with line-0 fallback. Captured in FR-009 (`get_eval_diagnostics`) and FR-009a (server-side capture). | [Resolved] | — | **Closed** |
| OQ-005 | **`server.py` `TextDocumentSyncKind` upgrade:** Resolved. Upgrading to `TextDocumentSyncKind.Full` is confirmed compatible with the existing Eglot configuration. Proceed with the upgrade as specified in FR-015. | [Resolved] | Phase 1 | **Closed** |

---

## 15. Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-23 | Requirements Analyst AI | Initial draft based on codebase exploration and three-phase stakeholder discovery |

---

## Appendix A: Glossary

| Term | Definition |
|------|------------|
| Parse tree | The concrete syntax tree produced by tree-sitter from a source document. Unlike an AST, it includes every token including whitespace and error nodes. |
| `DocumentCache` | The stateful in-process cache in `parser.py` mapping document URI to `(version, tree)`. |
| Error node | A tree-sitter node of type `ERROR` produced when the parser cannot match a grammar rule. The tree is still returned (tree-sitter is error-tolerant) but `tree.root_node.has_error` is `True`. |
| Missing node | A tree-sitter node where `node.is_missing` is `True` — a node the grammar expected but did not find in the input (e.g., a missing closing paren). |
| `LoadStatement` | A structured representation of a Starlark `load()` call extracted by `parser.py`, normalised across single-line and multi-line forms. |
| Semantic tokens | LSP capability (`textDocument/semanticTokens/full`) allowing the server to classify every token in a document by type and modifier for richer editor highlighting. |
| `starlarkish` evaluator | The internal Python execution engine at `common/python/starlarkish/` that evaluates `.mlody` files in a sandboxed Python environment. It is the semantic layer; tree-sitter is the syntactic layer. |
| Delta encoding | The LSP semantic tokens encoding format where each token is expressed as offsets relative to the previous token rather than absolute positions. |

---

## Appendix B: Relevant Existing Files

| File | Path | Relevance |
|------|------|-----------|
| LSP server | `mlody/lsp/server.py` | Entry point for all handler registrations |
| Completion provider | `mlody/lsp/completion.py` | Will be refactored (FR-004, FR-005) |
| Definition provider | `mlody/lsp/definition.py` | Will be refactored (FR-006) |
| LSP BUILD | `mlody/lsp/BUILD.bazel` | Needs new source files and pip deps added |
| LSP future work | `mlody/lsp/FUTURE.md` | Documents multi-line `load()` as known limitation |
| Emacs Starlark mode | `devex/editors/emacs/extensions/extensions/neo/mlody/neo-mlody-mode.el` | Reference for grammar source URL; unaffected by this change |
| Emacs grammar `.so` | `devex/editors/emacs/tree-sitter/libtree-sitter-starlark.so` | Compiled grammar for Emacs; distinct from the Python package |
| starlarkish evaluator | `common/python/starlarkish/evaluator/evaluator.py` | `_module_globals` used by hover (FR-011) |
| Test utilities | `common/python/starlarkish/evaluator/testing.py` | `InMemoryFS` for filesystem-mocking in tests |
| pyproject.toml | `pyproject.toml` | Add `tree-sitter` and `tree-sitter-starlark` here |

---

**End of Requirements Document**
