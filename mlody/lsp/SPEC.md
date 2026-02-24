# SPEC: mlody LSP — Incremental Updates

**Version:** 1.0 **Date:** 2026-02-23 **Status:** Approved

---

## Executive Summary

This specification covers two tightly coupled changes to the mlody LSP server:

1. **Incremental sync** — switch `TextDocumentSyncKind` from `Full` to
   `Incremental`, apply LSP range-diffs to a maintained document buffer, and
   pass the previous `tree_sitter.Tree` as `old_tree` to every re-parse.
2. **Same-file unsaved symbol extraction** — after each re-parse, walk the
   `module` root's direct children to collect completed top-level binding names
   and merge them into `_general_completions` so newly typed symbols appear as
   completions before the file is saved.

All existing LSP features (completion, go-to-definition, hover, diagnostics,
semantic tokens) must remain correct under the new sync mode.

**Requirements traceability:** FR-001 through FR-007 from `REQUIREMENTS.md` are
all addressed; NFR-001 through NFR-011 are met by the design choices in this
spec. Deferred scope items from `FUTURE.md` are unaffected.

---

## Architecture Overview

```
LSP client (Eglot)
  │
  │  textDocument/didChange
  │  content_changes: list[TextDocumentContentChangePartial]
  ▼
server.py: on_did_change()
  │
  │  apply_incremental_changes(old_text, changes) → new_text   [parser.py]
  │
  ▼
DocumentCache.update(uri, version, new_text)            [parser.py]
  │  reads (version, text, tree) from _cache
  │  calls _parser.parse(new_bytes, old_tree=old_tree)
  │  stores (version, new_text, new_tree) in _cache
  │
  ├─► _publish_diagnostics_for(uri, version, new_text)  [server.py]
  │     get_parse_diagnostics(tree) + get_eval_diagnostics(...)
  │
  └─► (on completion request)
        DocumentCache.get_tree(uri) → tree
        extract_top_level_symbols(tree) → list[str]     [parser.py]
        _general_completions(evaluator, file, tree)     [completion.py]
          = SAFE_BUILTINS ∪ evaluator globals ∪ extracted names
```

**Affected files (all changes are additive or in-place):**

| File                           | Change type                                                                              |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `mlody/lsp/parser.py`          | Extend `DocumentCache`; add `apply_incremental_changes`; add `extract_top_level_symbols` |
| `mlody/lsp/server.py`          | Switch sync kind; rewrite `on_did_change`                                                |
| `mlody/lsp/completion.py`      | Change signature + body of `_general_completions`; update `get_completions` caller       |
| `mlody/lsp/parser_test.py`     | Add test classes for new functions                                                       |
| `mlody/lsp/completion_test.py` | Add test cases for new `_general_completions` signature                                  |
| `mlody/lsp/server_test.py`     | Add incremental round-trip test                                                          |
| `mlody/lsp/BUILD.bazel`        | No change required                                                                       |

---

## Detailed Component Specifications

### 1. `apply_incremental_changes` — `parser.py`

#### Purpose

Pure function. Applies an ordered list of LSP range-edits to a full document
string, producing the updated full text. Must be implemented with no server
state so it is independently testable (NFR-010).

#### Signature

```python
def apply_incremental_changes(
    text: str,
    changes: list[
        types.TextDocumentContentChangePartial
        | types.TextDocumentContentChangeWholeDocument
    ],
) -> str:
```

#### Contract

- `text` is the current full document content (UTF-8 string).
- `changes` is the `content_changes` list from `DidChangeTextDocumentParams`,
  already in the order the client sent them.
- Returns the full document text after all changes have been applied.

#### Algorithm

```
for change in changes:
    if change has no .range attribute, or change.range is None:
        # TextDocumentContentChangeWholeDocument — treat as full replacement
        text = change.text
        continue

    # change is TextDocumentContentChangePartial
    start_line  = change.range.start.line
    start_char  = change.range.start.character
    end_line    = change.range.end.line
    end_char    = change.range.end.character

    lines = text.split("\n")

    # Extract the prefix on the start line up to start_char
    prefix = lines[start_line][:start_char]

    # Extract the suffix on the end line from end_char onwards
    # Guard against end_line being past the end of the document
    if end_line < len(lines):
        suffix = lines[end_line][end_char:]
    else:
        suffix = ""

    # The replacement text may contain newlines
    new_lines = (prefix + change.text + suffix).split("\n")

    # Reassemble: lines before start_line + new_lines + lines after end_line
    lines = lines[:start_line] + new_lines + lines[end_line + 1:]
    text = "\n".join(lines)

return text
```

**Performance:** For a single-character edit, only the affected line(s) are
touched; the slice operations are O(lines_in_edit), satisfying NFR-002.

**Correctness invariant (NFR-004):** After any sequence of incremental edits
starting from the same initial text, the result must equal the text a Full-sync
client would have sent for the same final buffer state.

#### Type note

`types.TextDocumentContentChangePartial` has a `.range` attribute of type
`types.Range`. `types.TextDocumentContentChangeWholeDocument` does not. The
union annotation keeps `basedpyright` strict mode satisfied; the `hasattr` check
at runtime provides the defensive fallback required by FR-002.

---

### 2. `DocumentCache` — `parser.py`

#### Current state

`_cache: dict[str, tuple[int, tree_sitter.Tree]]` — stores `(version, tree)`.

#### Required change (FR-003)

Extend the cache to store `(version, text, tree)` triples. The stored `text` is
always the post-diff, pre-next-change full document content.

#### New internal type

```python
# Replace the existing _cache field type:
_cache: dict[str, tuple[int, str, tree_sitter.Tree]]
```

#### Modified method: `update`

```python
def update(
    self,
    uri: str,
    version: int,
    text: str,
) -> tree_sitter.Tree:
```

**Contract changes:**

- If a cached entry exists for `uri` with the same `version`, return the cached
  tree immediately (unchanged behaviour).
- Otherwise, retrieve the previous tree (if any) from `_cache[uri][2]`.
- Call `_parser.parse(text.encode(), old_tree=prev_tree)` when `prev_tree` is
  not `None` (FR-004); call `_parser.parse(text.encode())` on first parse.
- Store `(version, text, new_tree)` in `_cache[uri]`.
- Return `new_tree`.

**Pseudo-code:**

```python
def update(self, uri: str, version: int, text: str) -> tree_sitter.Tree:
    cached = self._cache.get(uri)
    if cached is not None and cached[0] == version:
        return cached[2]  # index shifts: was [1], now [2]

    prev_tree: tree_sitter.Tree | None = cached[2] if cached is not None else None
    new_tree = (
        _parser.parse(text.encode(), old_tree=prev_tree)
        if prev_tree is not None
        else _parser.parse(text.encode())
    )
    self._cache[uri] = (version, text, new_tree)
    return new_tree
```

#### Modified method: `get`

```python
def get(self, uri: str) -> tree_sitter.Tree | None:
    cached = self._cache.get(uri)
    return cached[2] if cached is not None else None  # index shifts to [2]
```

#### New method: `get_text`

```python
def get_text(self, uri: str) -> str | None:
    """Return the last-stored full document text for *uri*, or None if not cached."""
    cached = self._cache.get(uri)
    return cached[1] if cached is not None else None
```

#### Unchanged methods

`remove` is unchanged in behaviour (pops the URI key regardless of stored tuple
shape; no index access is needed).

---

### 3. `extract_top_level_symbols` — `parser.py`

#### Purpose

Pure function over `tree_sitter.Tree` with no side effects (NFR-011). Walks the
direct children of the `module` root node and collects identifier names from
syntactically complete top-level assignments and `function_definition` nodes.

#### Signature

```python
def extract_top_level_symbols(tree: tree_sitter.Tree) -> list[str]:  # type: ignore[type-arg]
```

#### Contract

- Input: any `tree_sitter.Tree` (may contain ERROR nodes).
- Output: `list[str]` of symbol names defined at the top level of the document,
  in source order.
- Names beginning with `_` are excluded (private-by-convention, FR-005).
- Names in `_FRAMEWORK_INTERNALS` (imported from `completion.py`) are excluded.
- Nodes with `has_error == True` or of type `"ERROR"` are skipped entirely,
  including their children (NFR-005).

#### Algorithm

```
symbols = []
root = tree.root_node
for child in root.children:
    if child.type == "ERROR" or child.has_error:
        continue

    if child.type == "assignment":
        # LHS is children[0] for simple assignments ("X = ...")
        if child.child_count > 0:
            lhs = child.children[0]
            if lhs.type == "identifier":
                name = (lhs.text or b"").decode()
                if name and not name.startswith("_")
                        and name not in _FRAMEWORK_INTERNALS:
                    symbols.append(name)

    elif child.type == "function_definition":
        # Function name is children[1] — verified in CLAUDE.md
        if len(child.children) > 1:
            name_node = child.children[1]
            if name_node.type == "identifier":
                name = (name_node.text or b"").decode()
                if name and not name.startswith("_")
                        and name not in _FRAMEWORK_INTERNALS:
                    symbols.append(name)

    # expression_statement (e.g. load() calls) and other statement types
    # are ignored — they do not introduce bindings in the current file's scope

return symbols
```

#### Key AST facts (from `CLAUDE.md`)

- `assignment` node: LHS identifier is `children[0]`.
- `function_definition` node: name identifier is `children[1]`.
- Root node type is `"module"` in tree-sitter-starlark.
- `child.has_error` is `True` if the child or any descendant is an ERROR node —
  this is the primary guard for NFR-005.

#### Import dependency

`_FRAMEWORK_INTERNALS` is defined in `completion.py`. To avoid a circular
import, move the constant to `parser.py` (or to a new `_constants.py`). The
recommended approach is to move it to `parser.py` since
`extract_top_level_symbols` lives there and `completion.py` already imports from
`parser.py`.

**Migration plan:**

1. Move `_FRAMEWORK_INTERNALS` from `completion.py` to `parser.py`.
2. Add to `completion.py`:
   ```python
   from mlody.lsp.parser import _FRAMEWORK_INTERNALS
   ```
   (or keep a local re-import alias — either approach satisfies strict mode).

---

### 4. Changes to `server.py`

#### 4a. Switch sync kind (FR-001)

Replace:

```python
server = LanguageServer(
    "mlody-lsp",
    "v0.1",
    text_document_sync_kind=types.TextDocumentSyncKind.Full,
)
```

With:

```python
server = LanguageServer(
    "mlody-lsp",
    "v0.1",
    text_document_sync_kind=types.TextDocumentSyncKind.Incremental,
)
```

Update the comment above the constructor accordingly.

#### 4b. Rewrite `on_did_change` (FR-002)

**Current implementation:**

```python
@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(params: types.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    version = params.text_document.version
    # FULL sync: exactly one content change containing the complete document.
    text = params.content_changes[0].text if params.content_changes else ""
    _publish_diagnostics_for(uri, version, text)
```

**New implementation:**

```python
@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(params: types.DidChangeTextDocumentParams) -> None:
    """Re-parse the changed document and republish diagnostics.

    With TextDocumentSyncKind.Incremental the client sends only the changed
    ranges; apply_incremental_changes reconstructs the full text from the
    previous buffer stored in CACHE.
    """
    uri = params.text_document.uri
    version = params.text_document.version
    old_text = CACHE.get_text(uri) or ""
    new_text = apply_incremental_changes(old_text, list(params.content_changes))
    _publish_diagnostics_for(uri, version, new_text)
```

**New import required in `server.py`:**

```python
from mlody.lsp.parser import CACHE, apply_incremental_changes, find_ancestor, node_at_position
```

(`apply_incremental_changes` added to the existing import line.)

#### 4c. Remove stale comment

The comment
`# FULL sync is required so the client sends the complete document text in didChange content changes...`
above the `server = LanguageServer(...)` call must be removed or replaced with a
comment describing the incremental sync approach.

---

### 5. Changes to `completion.py`

#### 5a. Move `_FRAMEWORK_INTERNALS`

As described in section 3, move `_FRAMEWORK_INTERNALS` to `parser.py` and import
it back here:

```python
from mlody.lsp.parser import (
    _FRAMEWORK_INTERNALS,
    extract_top_level_symbols,
    find_ancestor,
    node_at_position,
)
```

#### 5b. New signature for `_general_completions` (FR-006)

**Current:**

```python
def _general_completions(evaluator: Evaluator, current_file: Path) -> list[str]:
```

**New:**

```python
def _general_completions(
    evaluator: Evaluator | None,
    current_file: Path,
    tree: tree_sitter.Tree,  # type: ignore[type-arg]
) -> list[str]:
```

**New body:**

```python
def _general_completions(
    evaluator: Evaluator | None,
    current_file: Path,
    tree: tree_sitter.Tree,  # type: ignore[type-arg]
) -> list[str]:
    """Return safe builtins, evaluator globals, and unsaved buffer symbols.

    Merges three sources (deduplicated by set union):
    1. SAFE_BUILTINS — always present.
    2. evaluator._module_globals[current_file] — last saved state (empty when
       evaluator is None or the file has not been evaluated).
    3. extract_top_level_symbols(tree) — completed bindings in the current
       unsaved buffer.

    _FRAMEWORK_INTERNALS and _-prefixed names are excluded from sources 2 and 3.
    """
    names: set[str] = set(SAFE_BUILTINS.keys())

    if evaluator is not None:
        module_globals: dict[str, object] = evaluator._module_globals.get(  # type: ignore[attr-defined]
            current_file, {}
        )
        for key in module_globals:
            if key not in _FRAMEWORK_INTERNALS and not key.startswith("_"):
                names.add(key)

    for sym in extract_top_level_symbols(tree):
        names.add(sym)  # already filtered by extract_top_level_symbols

    return list(names)
```

**Callers that must be updated:**

`get_completions` calls `_general_completions` in the `else` branch:

```python
# Old:
labels = _general_completions(evaluator, current_file)

# New:
labels = _general_completions(evaluator, current_file, tree)
```

#### 5c. Remove early-exit guard for `evaluator is None` in `get_completions`

The current `get_completions` function returns `[]` immediately if
`evaluator is None`. This must change so that tree-extracted symbols are still
returned when the evaluator failed to load (FR-006 rule: "If evaluator is None,
tree-extracted names are still returned").

**Current:**

```python
def get_completions(...) -> list[CompletionItem]:
    if evaluator is None:
        return []
    ...
    else:
        labels = _general_completions(evaluator, current_file)
```

**New:**

```python
def get_completions(...) -> list[CompletionItem]:
    # Do NOT return [] early for evaluator is None — tree-extracted symbols
    # must still be offered even when the workspace failed to load (FR-006).
    line_to_cursor = document_lines[line][:character] if line < len(document_lines) else ""
    node = node_at_position(tree, line, character)
    context = _detect_context(node, line_to_cursor)

    if context == "load_path":
        if evaluator is None:
            return []  # load path completions require evaluator's monorepo_root
        # ... existing load_path logic unchanged ...
    elif context == "load_symbol":
        labels = []
    elif context == "builtins_member":
        labels = _builtin_member_completions()
    else:
        labels = _general_completions(evaluator, current_file, tree)

    return [CompletionItem(label=name) for name in labels]
```

Note: `load_path` completions use `monorepo_root` (a `Path` derived from the
workspace) so they can safely remain guarded by `evaluator is None` — if the
workspace failed to load, the monorepo root may be meaningless. The guard must
only be removed from the `general` code path.

---

## Data Architecture

### `DocumentCache._cache` entry

| Field | Python type        | Description                                   |
| ----- | ------------------ | --------------------------------------------- |
| Key   | `str`              | LSP document URI                              |
| `[0]` | `int`              | LSP version counter                           |
| `[1]` | `str`              | Full document text after last `update()` call |
| `[2]` | `tree_sitter.Tree` | Parse tree corresponding to `[1]`             |

### Symbol name (extracted)

A plain `str`. No wrapper dataclass is required; `extract_top_level_symbols`
returns `list[str]` directly, consistent with the existing return types in
`_general_completions` and `_load_path_completions`.

---

## API Specifications

### `apply_incremental_changes`

```
Module:    mlody.lsp.parser
Exported:  Yes (imported by server.py)

apply_incremental_changes(
    text: str,
    changes: list[
        types.TextDocumentContentChangePartial
        | types.TextDocumentContentChangeWholeDocument
    ],
) -> str

Raises:    Never — malformed range indices are clipped by Python slice semantics.
           A completely invalid document (e.g. end_line > len(lines)) is handled
           by the `if end_line < len(lines)` guard; suffix defaults to "".
```

### `extract_top_level_symbols`

```
Module:    mlody.lsp.parser
Exported:  Yes (imported by completion.py)

extract_top_level_symbols(
    tree: tree_sitter.Tree,
) -> list[str]

Raises:    Never — skips all error nodes rather than raising.
```

### `DocumentCache.get_text`

```
Module:    mlody.lsp.parser
Class:     DocumentCache

get_text(self, uri: str) -> str | None

Returns:   Last-stored full document text, or None if uri is not in cache.
Raises:    Never.
```

---

## Implementation Plan

### Phase 1 — `parser.py` changes (no server integration yet)

1. Move `_FRAMEWORK_INTERNALS` constant from `completion.py` to `parser.py`.
2. Extend `DocumentCache._cache` type from `dict[str, tuple[int, Tree]]` to
   `dict[str, tuple[int, str, Tree]]`.
3. Update `DocumentCache.update`, `get`, and `__init__` to match the new tuple
   shape.
4. Add `DocumentCache.get_text` accessor.
5. Add `apply_incremental_changes` as a module-level function.
6. Add `extract_top_level_symbols` as a module-level function.

All changes in this phase are self-contained in `parser.py` and can be tested
before touching `server.py` or `completion.py`.

### Phase 2 — `completion.py` changes

1. Update import line to bring in `_FRAMEWORK_INTERNALS` and
   `extract_top_level_symbols` from `parser.py`.
2. Change signature of `_general_completions` to accept
   `evaluator: Evaluator | None` and `tree: tree_sitter.Tree`.
3. Update body of `_general_completions` per section 5b.
4. Update the single call site in `get_completions` (the `else` branch).
5. Remove the early-exit `if evaluator is None: return []` guard from
   `get_completions`, adding the more targeted guard for `load_path` only.

### Phase 3 — `server.py` changes

1. Switch `TextDocumentSyncKind.Full` to `TextDocumentSyncKind.Incremental`.
2. Add `apply_incremental_changes` to the import from `mlody.lsp.parser`.
3. Rewrite `on_did_change` body to call `CACHE.get_text` then
   `apply_incremental_changes`.
4. Remove/update stale comment above the `server` constructor.

### Phase 4 — tests

1. **`parser_test.py`** — add `TestApplyIncrementalChanges` and
   `TestExtractTopLevelSymbols` classes (see Testing Strategy).
2. **`completion_test.py`** — add `TestGeneralCompletionsWithTree` class; update
   existing `TestGetCompletions` tests that pass `evaluator=None` to confirm
   tree-extracted symbols are still returned.
3. **`server_test.py`** — add `TestIncrementalSyncRoundTrip` class.

### Dependency order

Phase 1 must complete before Phases 2 and 3 (they depend on the new exports from
`parser.py`). Phases 2 and 3 are independent of each other. Phase 4 can be
written in parallel with Phases 1–3, running once all phases are complete.

---

## Testing Strategy

All tests use `o_py_test` via the existing `BUILD.bazel` targets (no new Bazel
targets are required). Run with:

```sh
bazel test //mlody/lsp/... --test_output=errors
```

### `parser_test.py` — new test classes

#### `TestApplyIncrementalChanges`

Each test imports `apply_incremental_changes` from `mlody.lsp.parser` and uses
`types.TextDocumentContentChangePartial` /
`TextDocumentContentChangeWholeDocument` from `lsprotocol`.

| Test method                              | Scenario                                   | Assertion                       |
| ---------------------------------------- | ------------------------------------------ | ------------------------------- |
| `test_single_insertion`                  | Insert `"X"` at (0, 3) in `"abc\ndef\n"`   | Result == `"abcX\ndef\n"`       |
| `test_single_deletion`                   | Delete chars 0–1 on line 0 from `"abc\n"`  | Result == `"c\n"`               |
| `test_replacement`                       | Replace `"old"` (0, 0)–(0, 3) with `"new"` | Result == `"new\n"`             |
| `test_multiline_replacement`             | Replace lines 1–2 with a single line       | Lines[1:] correct               |
| `test_empty_changes_list`                | `changes=[]`                               | Returns original text unchanged |
| `test_full_text_fallback_no_range`       | `TextDocumentContentChangeWholeDocument`   | Returns `change.text`           |
| `test_multiple_changes_applied_in_order` | Two sequential partial changes             | Both applied in order           |
| `test_insertion_appending_newline`       | Insert `"\n"` at end of last line          | Line count increases by 1       |

**Implementation note for `test_single_insertion`:**

```python
from lsprotocol import types
from mlody.lsp.parser import apply_incremental_changes

def test_single_insertion(self) -> None:
    change = types.TextDocumentContentChangePartial(
        range=types.Range(
            start=types.Position(line=0, character=3),
            end=types.Position(line=0, character=3),
        ),
        text="X",
    )
    result = apply_incremental_changes("abc\ndef\n", [change])
    assert result == "abcX\ndef\n"
```

#### `TestExtractTopLevelSymbols`

Each test calls `extract_top_level_symbols` on a tree produced by parsing a
small source string via `DocumentCache().update(...)`.

| Test method                                     | Source                               | Expected result                  |
| ----------------------------------------------- | ------------------------------------ | -------------------------------- |
| `test_completed_assignment_extracted`           | `"MY_MODEL = struct(name='bert')\n"` | `["MY_MODEL"]`                   |
| `test_def_extracted`                            | `"def train():\n    pass\n"`         | `["train"]`                      |
| `test_incomplete_assignment_not_extracted`      | `"MY_MODEL = struct(\n"`             | `[]`                             |
| `test_nested_assignment_not_extracted`          | `"def f():\n    x = 1\n"`            | `[]` (no top-level bindings)     |
| `test_underscore_prefixed_not_extracted`        | `"_PRIVATE = 1\n"`                   | `[]`                             |
| `test_framework_internal_not_extracted`         | `"load = 1\n"` (if parseable)        | `[]`                             |
| `test_empty_file_returns_empty_list`            | `""`                                 | `[]`                             |
| `test_multiple_top_level_symbols`               | `"A = 1\nB = 2\ndef f(): pass\n"`    | `["A", "B", "f"]`                |
| `test_error_node_sibling_clean_symbol_included` | `"A = 1\ndef (\n"`                   | `["A"]` (ERROR sibling excluded) |

**Verification approach for `test_incomplete_assignment_not_extracted`:**

Parse `"MY_MODEL = struct(\n"` — tree-sitter-starlark ERROR-recovers this as an
assignment with `has_error == True`. The spec's guard on `child.has_error` must
exclude it.

**Verification approach for `test_error_node_sibling_clean_symbol_included`:**

Parse `"A = 1\ndef (\n"`. The first child is a clean `assignment` for `A`; the
second child is an ERROR node for `def (`. The function must skip the second
child but include `A`.

#### `TestDocumentCacheGetText`

| Test method                                | Scenario                              | Assertion                        |
| ------------------------------------------ | ------------------------------------- | -------------------------------- |
| `test_get_text_returns_none_before_update` | New cache, URI not seen               | `cache.get_text(uri) is None`    |
| `test_get_text_returns_stored_text`        | After `cache.update(uri, 1, "abc\n")` | `cache.get_text(uri) == "abc\n"` |
| `test_get_text_updates_on_version_change`  | Update v1 then v2 with different text | Returns text from v2             |

#### `TestDocumentCacheIncrementalReparse`

Verify that `old_tree` is passed through on re-parse. Because tree-sitter's
`parse` signature is C-level and hard to mock, this test verifies the observable
outcome: the returned tree for v2 is different from v1, and both are valid
`tree_sitter.Tree` instances.

| Test method                                                 | Scenario                                  | Assertion                                   |
| ----------------------------------------------------------- | ----------------------------------------- | ------------------------------------------- |
| `test_new_version_passes_old_tree_and_returns_correct_tree` | Parse v1 `"x = 1\n"`, then v2 `"y = 2\n"` | v2 tree root text contains `"y"`, not `"x"` |

### `completion_test.py` — new test class `TestGeneralCompletionsWithTree`

```python
from mlody.lsp.parser import DocumentCache
from mlody.lsp.completion import _general_completions

def _tree(src: str) -> tree_sitter.Tree:
    return DocumentCache().update("file:///t.mlody", 1, src)
```

| Test method                         | Scenario                                                       | Assertion                                     |
| ----------------------------------- | -------------------------------------------------------------- | --------------------------------------------- |
| `test_evaluator_only_no_unsaved`    | Evaluator has `MY_CONFIG`; tree is `"x = 1\n"`                 | `"MY_CONFIG"` in result, `"x"` also in result |
| `test_tree_only_evaluator_none`     | `evaluator=None`; tree is `"MY_MODEL = struct(name='bert')\n"` | `"MY_MODEL"` in result                        |
| `test_both_sources_deduplication`   | Evaluator has `MY_MODEL`; tree also defines `MY_MODEL`         | `"MY_MODEL"` appears exactly once             |
| `test_error_node_symbol_absent`     | `evaluator=None`; tree is `"MY_MODEL = struct(\n"`             | `"MY_MODEL"` NOT in result                    |
| `test_safe_builtins_always_present` | Any combination                                                | All `SAFE_BUILTINS` keys in result            |

**Update to existing tests:**

`TestGetCompletions.test_returns_empty_list_when_evaluator_is_none` must be
updated. Under the new design, `get_completions` with `evaluator=None` no longer
returns `[]` for the `general` context — it returns tree-extracted symbols. The
test should either:

- Pass a tree with no top-level symbols (the current `"struct("` source has an
  ERROR node, so `extract_top_level_symbols` returns `[]`) and assert that
  `SAFE_BUILTINS` items are present; or
- Be renamed to reflect the updated semantics.

The recommended update is:

```python
def test_returns_builtins_when_evaluator_is_none(self) -> None:
    src = "struct("  # ERROR node — no extracted symbols
    tree = CACHE.update("file:///gc_test1.mlody", 1, src)
    result = get_completions(
        evaluator=None,
        monorepo_root=Path("/repo"),
        current_file=Path("/repo/mlody/file.mlody"),
        tree=tree,
        line=0,
        character=7,
        document_lines=[src],
    )
    labels = [item.label for item in result]
    for key in SAFE_BUILTINS:
        assert key in labels
```

### `server_test.py` — new test class `TestIncrementalSyncRoundTrip`

```python
class TestIncrementalSyncRoundTrip:
    """Requirement: After incremental edits the cache holds the correct text
    and completion returns updated symbols (KPI-001, NFR-004).
    """

    def test_did_change_partial_updates_cache_text(self) -> None:
        """After a partial edit, CACHE.get_text returns the new full text."""
        uri = "file:///test_incremental_rt.mlody"
        initial_text = "A = 1\n"

        # Seed the cache as if didOpen was called
        server_module.CACHE.update(uri, version=1, text=initial_text)

        # Simulate a partial edit: insert "B = 2\n" at line 1, char 0
        change = types.TextDocumentContentChangePartial(
            range=types.Range(
                start=types.Position(line=1, character=0),
                end=types.Position(line=1, character=0),
            ),
            text="B = 2\n",
        )
        params = types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=2),
            content_changes=[change],
        )

        with patch.object(
            server_module.server, "text_document_publish_diagnostics"
        ):
            on_did_change(params)

        result = server_module.CACHE.get_text(uri)
        assert result == "A = 1\nB = 2\n"

    def test_unsaved_symbol_appears_in_completion_after_did_change(self) -> None:
        """KPI-001: MY_MODEL typed in unsaved buffer appears in completion list."""
        uri = "file:///test_incremental_sym.mlody"
        initial_text = ""

        server_module.CACHE.update(uri, version=1, text=initial_text)

        change = types.TextDocumentContentChangePartial(
            range=types.Range(
                start=types.Position(line=0, character=0),
                end=types.Position(line=0, character=0),
            ),
            text='MY_MODEL = struct(name="bert")\n',
        )
        did_change_params = types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=2),
            content_changes=[change],
        )

        with patch.object(
            server_module.server, "text_document_publish_diagnostics"
        ):
            on_did_change(did_change_params)

        # Now request completion from a position on a new line
        # The tree now contains MY_MODEL = struct(...) which is syntactically complete
        completion_src = 'MY_MODEL = struct(name="bert")\nMY_'
        tree = server_module.CACHE.get(uri)
        assert tree is not None

        # _general_completions with evaluator=None should still surface MY_MODEL
        from mlody.lsp.completion import _general_completions
        labels = _general_completions(
            evaluator=None,
            current_file=Path(to_fs_path(uri) or uri),  # type: ignore[arg-type]
            tree=tree,
        )
        assert "MY_MODEL" in labels

    def test_incomplete_assignment_not_in_completion(self) -> None:
        """NFR-005: MY_MODEL = struct( (no closing paren) does NOT appear in completions."""
        uri = "file:///test_incremental_incomplete.mlody"
        incomplete_text = "MY_MODEL = struct(\n"
        server_module.CACHE.update(uri, version=1, text=incomplete_text)

        tree = server_module.CACHE.get(uri)
        assert tree is not None

        from mlody.lsp.completion import _general_completions
        labels = _general_completions(
            evaluator=None,
            current_file=Path(to_fs_path(uri) or uri),  # type: ignore[arg-type]
            tree=tree,
        )
        assert "MY_MODEL" not in labels
```

---

## Security and Compliance

No new attack surface. `apply_incremental_changes` and
`extract_top_level_symbols` are pure in-memory functions that do not touch the
filesystem. Existing sandbox isolation (no stdout writes, `_noop_print`) is
unchanged. No new third-party dependencies are introduced (FR-002 constraint).

---

## Non-Functional Requirements Compliance

| NFR                                        | Compliance mechanism                                                                                          |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| NFR-001 (200 ms latency)                   | Incremental re-parse avoids full re-parse on every keystroke; `apply_incremental_changes` is O(changed lines) |
| NFR-002 (O(lines changed))                 | Algorithm splits on `"\n"`, slices prefix/suffix of affected lines only                                       |
| NFR-003 (old_tree used on every didChange) | `DocumentCache.update` passes `prev_tree` whenever `_cache[uri]` exists                                       |
| NFR-004 (text identity)                    | `apply_incremental_changes` tested with round-trip assertions                                                 |
| NFR-005 (no incomplete symbols)            | `child.has_error` guard in `extract_top_level_symbols`                                                        |
| NFR-006 (existing features unchanged)      | `_publish_diagnostics_for` receives the reconstructed full text; all other handlers unchanged                 |
| NFR-007 (basedpyright strict)              | Union annotation on `changes` param; `# type: ignore[type-arg]` where tree-sitter stubs are incomplete        |
| NFR-008 (ruff)                             | No new patterns introduced                                                                                    |
| NFR-009 (test coverage)                    | See Testing Strategy above                                                                                    |
| NFR-010 (pure diff function)               | `apply_incremental_changes` has no server-state access                                                        |
| NFR-011 (pure extraction function)         | `extract_top_level_symbols` has no side effects                                                               |

---

## Open Questions Resolved

| ID     | Resolution                                                                                                                                                         |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| OQ-001 | Eglot sends one entry per keystroke; ordered application in `apply_incremental_changes` handles batched lists too — no special casing needed                       |
| OQ-002 | pygls dispatches on a single asyncio event-loop thread; `_parser.parse()` is never called concurrently; no additional locking required                             |
| OQ-003 | `_general_completions` callers: exactly one call site in `get_completions` (the `else` branch). No callers exist outside `completion.py`. Signature change is safe |

---

## Risks and Mitigation

| Risk                                                                                   | Likelihood | Impact | Mitigation                                                                                                                                         |
| -------------------------------------------------------------------------------------- | ---------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apply_incremental_changes` produces wrong text for multi-codepoint Unicode characters | Low        | High   | Requirement 2.3 states UTF-8 and LSP character offsets coincide for ASCII-safe Starlark; no surrogate handling needed; test with ASCII inputs only |
| Old `test_returns_empty_list_when_evaluator_is_none` test breaks                       | Certain    | Low    | Test update is specified in the Testing Strategy section; rename and update assertions                                                             |
| tree-sitter `has_error` not set on parent when only child is ERROR                     | Low        | Medium | tree-sitter guarantees `has_error` propagates upward; confirmed in CLAUDE.md notes on `node.has_error`                                             |
| `_FRAMEWORK_INTERNALS` move causes import cycle                                        | Low        | Medium | `completion.py` already imports from `parser.py`; moving the constant to `parser.py` is a one-way dependency                                       |

---

## Future Considerations

Items explicitly deferred per `FUTURE.md`:

- **Cross-file unsaved buffer completions** — after this change, the
  `DocumentCache` already stores per-URI text. Extending to cross-file tree
  extraction or starlarkish re-evaluation is the natural next step.
- **Smarter on-save workspace reload** — `on_changed_watched_files` full reload
  is unchanged; incremental invalidation requires a dependency graph.
- **`Evaluator.get_module_globals` public accessor** — `completion.py` still
  accesses `evaluator._module_globals` directly. This cleanup should be
  addressed in a follow-up.
