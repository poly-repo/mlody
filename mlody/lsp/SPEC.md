# SPEC.md: Starlark Tree-Sitter Parsing for mlody LSP

**Issue:** #388
**Status:** Ready for implementation
**Date:** 2026-02-23
**Requirements source:** `mlody/lsp/REQUIREMENTS.md` (all open questions resolved)

---

## 1. Overview

### What Is Being Built

This feature integrates the Python `tree-sitter` library and the `tree-sitter-starlark` grammar into the mlody Language Server Protocol server. It replaces the existing single-line regex parsing in `completion.py` and `definition.py` with a central, stateful parse-tree cache in a new `parser.py` module. Three new LSP capabilities are added: diagnostics (parse errors and evaluator errors as editor squiggles), hover (evaluated values, load path resolution, and node-type fallback), and semantic tokens (token classification for editors that cannot perform their own highlighting).

### Why

The current regex approach cannot reason across multiple lines, provides no syntax error feedback, and offers no foundation for hover or semantic tokens. Starlark `load()` calls that span multiple lines are silently ignored, breaking both completions and go-to-definition for a common real-world pattern. The `tree-sitter` library produces a concrete syntax tree from the full document text, solving all four structural deficiencies in a single architectural change.

### Outcome

- Live syntax error squiggles on every `didOpen` and `didChange`
- Hover showing evaluated symbol values, resolved load paths, and node-type fallback
- Semantic token data for editors without native tree-sitter support
- Correct multi-line `load()` handling in completions and go-to-definition
- `parser.py` as a documented extension point for all future parse-tree-dependent LSP capabilities

---

## 2. Architecture

### 2.1 Module Dependency Graph

```
server.py
  │
  ├── parser.py           NEW  tree-sitter wrapper + DocumentCache singleton
  │     imported by all feature handlers
  │
  ├── diagnostics.py      NEW  pure functions: parse diagnostics + eval diagnostics
  │     no server state; called by server.py only
  │
  ├── completion.py       MODIFIED  tree-sitter context detection replaces regex
  │
  ├── definition.py       MODIFIED  tree-sitter load extraction replaces regex
  │
  └── log_handler.py      UNCHANGED
```

### 2.2 Cache Lifecycle (sequence view)

```
didOpen(uri, version=1, text)
  -> parser.CACHE.update(uri, 1, text)          # parse and cache
  -> diagnostics.get_parse_diagnostics(tree)    # tree-sitter errors
  -> diagnostics.get_eval_diagnostics(exc, uri) # evaluator errors (if _eval_error set)
  -> server.publish_diagnostics(uri, merged)

didChange(uri, version=2, full_text)
  -> parser.CACHE.update(uri, 2, full_text)     # re-parse; version gating skips if same
  -> diagnostics.get_parse_diagnostics(tree)
  -> diagnostics.get_eval_diagnostics(exc, uri)
  -> server.publish_diagnostics(uri, merged)

didClose(uri)
  -> parser.CACHE.remove(uri)                   # evict; GC tree

completion(uri, position)
  -> parser.CACHE.get(uri)                      # cache hit -> no re-parse
  -> if None: eager parse via workspace.get_text_document(uri).source
  -> parser.node_at_position(tree, ...)
  -> context detection -> completions

hover(uri, position)
  -> parser.CACHE.get(uri)                      # cache hit
  -> if None: eager parse
  -> priority: load_path -> eval_globals -> node_type fallback

definition(uri, position)
  -> parser.CACHE.get(uri)                      # cache hit
  -> if None: eager parse
  -> parser.get_load_statements(tree)
  -> resolve path or symbol

semanticTokens/full(uri)
  -> parser.CACHE.get(uri)                      # cache hit
  -> walk tree, classify nodes, delta-encode
```

### 2.3 Cache Miss Pattern

All handlers use this idiom when the URI has not been opened via `didOpen`:

```python
tree = parser.CACHE.get(uri)
if tree is None:
    doc = server.workspace.get_text_document(uri)
    tree = parser.CACHE.update(uri, 0, doc.source)
```

Version `0` is used as a sentinel for eagerly-parsed documents not yet tracked by the client. A subsequent `didOpen` with version `1` will replace it.

---

## 3. Module Specifications

### 3.1 `mlody/lsp/parser.py` (NEW)

**Purpose:** Central stateful parse-tree cache. Owns grammar loading and all tree traversal helpers. Every LSP handler imports from this module.

#### 3.1.1 Module-Level Singletons

```python
import tree_sitter_starlark
from tree_sitter import Language, Parser

STARLARK_LANGUAGE: Language = Language(tree_sitter_starlark.language())
_parser: Parser = Parser(STARLARK_LANGUAGE)
CACHE: DocumentCache = DocumentCache()
```

`STARLARK_LANGUAGE` and `_parser` are initialized at import time. Grammar loading is expensive and must not happen per-request. If `tree_sitter_starlark` is not installed, the `ImportError` propagates immediately at server startup with the message:

```
ImportError: tree-sitter-starlark is not installed. Run: o-repin
```

The module should wrap the import in a try/except at the top level and re-raise with that message.

`CACHE` is a module-level singleton so all handlers share the same document state without threading concerns (pygls dispatches handlers sequentially on the asyncio event loop).

#### 3.1.2 `DocumentCache` Class

```python
class DocumentCache:
    _cache: dict[str, tuple[int, tree_sitter.Tree]]

    def update(self, uri: str, version: int, text: str) -> tree_sitter.Tree:
        """Parse text and store in cache. Returns cached tree if version unchanged."""

    def get(self, uri: str) -> tree_sitter.Tree | None:
        """Return the cached tree for uri, or None on cache miss."""

    def remove(self, uri: str) -> None:
        """Evict the cached entry for uri."""
```

**Version gating:** If `update()` is called with the same `version` as the cached entry, the existing tree is returned immediately — no re-parse. This prevents redundant parses when multiple handlers run in the same request cycle.

**Parsing:** `_parser.parse(text.encode())` — tree-sitter requires `bytes`, not `str`. The encoding is always UTF-8.

**Error tolerance:** tree-sitter always returns a tree, even for documents with syntax errors. `tree.root_node.has_error` is `True` when errors exist; `update()` stores and returns this tree normally. Error detection is the responsibility of `diagnostics.py`, not `DocumentCache`.

#### 3.1.3 Dataclasses

```python
@dataclass(frozen=True)
class ImportedSymbol:
    name: str
    node: tree_sitter.Node   # the string literal node for this symbol name

@dataclass(frozen=True)
class LoadStatement:
    path: str                   # e.g. "//mlody/core/builtins.mlody"
    path_node: tree_sitter.Node # the string literal node for the path
    symbols: list[ImportedSymbol]
```

These are frozen dataclasses. `node` fields hold references to tree-sitter `Node` objects. Per R-002 in the requirements, handlers must not store raw `Node` objects beyond request scope — the tree may be replaced by a subsequent `didChange`.

`tree_sitter.Node` has no stable type stubs; mark the `node` field with `# type: ignore[type-arg]` if basedpyright complains about the type argument, and add an inline comment referencing NFR-001.

#### 3.1.4 Helper Functions

**`node_at_position(tree, line, character) -> tree_sitter.Node`**

Returns the deepest (most specific) node whose source range contains the given LSP position. LSP positions are 0-indexed line and character.

Algorithm: start at `tree.root_node`, then walk into the child whose range contains the position, repeating until no child range contains the point. Use `node.children` iteration — not `node.named_children` — to include anonymous nodes (punctuation, keywords).

tree-sitter node positions are `(row, column)` tuples matching LSP `(line, character)` directly (both 0-indexed).

If the position is past the end of the document, return the root node.

**`find_ancestor(node, type_name) -> tree_sitter.Node | None`**

Walk `node.parent` links until a node of the given `type_name` is found, or the root is reached. Returns `None` if no ancestor of that type exists.

```python
def find_ancestor(node: tree_sitter.Node, type_name: str) -> tree_sitter.Node | None:
    current = node.parent
    while current is not None:
        if current.type == type_name:
            return current
        current = current.parent
    return None
```

**`get_load_statements(tree) -> list[LoadStatement]`**

Traverse the parse tree depth-first and collect all nodes of type `"call"` (tree-sitter-starlark grammar term for function calls) where the function node is an `"identifier"` with text `"load"`.

For each such call node:
1. The first argument is the path string — extract its text, stripping the surrounding quotes.
2. All subsequent arguments are symbol names — extract their text, stripping quotes.
3. Construct `LoadStatement(path=..., path_node=..., symbols=[...])`.

This traversal handles multi-line `load()` calls transparently because tree-sitter builds the tree from the full document — line boundaries do not exist at the AST level.

The grammar node structure for a `load()` call in `tree-sitter-starlark`:
```
(call
  function: (identifier)     ; text = "load"
  arguments: (argument_list
    (string)                 ; path argument
    (string)                 ; symbol 1
    (string)                 ; symbol 2
    ...
  )
)
```

Use a recursive depth-first walk (or tree-sitter's `tree.root_node.walk()` cursor API) to find all `call` nodes.

---

### 3.2 `mlody/lsp/diagnostics.py` (NEW)

**Purpose:** Pure functions that convert tree-sitter parse errors and evaluator exceptions into `lsprotocol.types.Diagnostic` objects. No server state; no side effects.

#### 3.2.1 `get_parse_diagnostics`

```python
def get_parse_diagnostics(tree: tree_sitter.Tree) -> list[lsprotocol.types.Diagnostic]:
```

Walk the parse tree depth-first. For each node where `node.is_error` is `True` or `node.is_missing` is `True`, emit a `Diagnostic`:

| Field | Value |
|---|---|
| `range` | `Range(start=Position(line=node.start_point[0], character=node.start_point[1]), end=Position(line=node.end_point[0], character=node.end_point[1]))` |
| `severity` | `DiagnosticSeverity.Error` |
| `message` | `f"Syntax error: unexpected '{node.type}'"` for error nodes; `f"Syntax error: missing '{node.type}'"` for missing nodes |
| `source` | `"mlody-lsp"` |

**Early exit optimization:** If `not tree.root_node.has_error`, return `[]` immediately — no traversal needed.

**Traversal strategy:** Use a stack-based depth-first walk over `node.children`. Do not recurse into the children of an `ERROR` node — a single diagnostic per error subtree is sufficient and avoids flooding.

#### 3.2.2 `get_eval_diagnostics`

```python
def get_eval_diagnostics(exc: Exception, uri: str) -> list[lsprotocol.types.Diagnostic]:
```

Converts a single Python exception (from `Workspace.load()`) into a one-element diagnostic list.

**`SyntaxError` handling:**
- `exc.lineno` is 1-indexed; convert to 0-indexed: `line = (exc.lineno or 1) - 1`
- `exc.offset` is 1-indexed character offset; convert: `char = (exc.offset or 1) - 1`
- `message = f"SyntaxError: {exc.msg}"`
- Range: `Range(start=Position(line=line, character=char), end=Position(line=line, character=char))`

**All other exceptions (`NameError`, `ImportError`, `Exception`):**
- Inspect `exc.__traceback__` for the innermost frame whose `tb_frame.f_code.co_filename` matches the document's filesystem path.
- Convert `uri` to filesystem path using `pygls.uris.to_fs_path(uri)` for the comparison.
- If a matching frame is found, use `tb_lineno - 1` (0-indexed) as the line; character is `0`.
- If no matching frame is found (e.g., the error is in the evaluator internals), fall back to `line=0, character=0`.
- `message = f"{type(exc).__name__}: {exc}"`
- `severity`: `DiagnosticSeverity.Error`, `source`: `"mlody-lsp"`

**Traceback walk:**

```python
tb = exc.__traceback__
doc_path = to_fs_path(uri) or ""
while tb is not None:
    if tb.tb_frame.f_code.co_filename == doc_path:
        line = tb.tb_lineno - 1
        break
    tb = tb.tb_next
else:
    line = 0
```

---

### 3.3 `mlody/lsp/server.py` (MODIFIED)

#### 3.3.1 New State

Add one module-level variable alongside `_evaluator`:

```python
_eval_error: Exception | None = None  # set when Workspace.load() raises
```

#### 3.3.2 Updated `on_initialized` and `on_changed_watched_files`

Both handlers that call `Workspace.load()` must be updated to store the exception on failure:

```python
# On success:
_evaluator = workspace.evaluator
_eval_error = None

# On failure (in except block):
_evaluator = None
_eval_error = exc   # store the captured exception
```

The `except Exception as exc` clause must bind the exception to `exc` (currently it is `except Exception` with no binding).

#### 3.3.3 `TextDocumentSyncKind` Upgrade

Change the `LanguageServer` server capabilities to declare `FULL` sync. In pygls, this is done at feature registration time via the `INITIALIZED` or `TEXT_DOCUMENT_DID_OPEN` sync options. The canonical pygls pattern is:

```python
server = LanguageServer(
    "mlody-lsp",
    "v0.1",
    text_document_sync_kind=types.TextDocumentSyncKind.Full,
)
```

If the constructor does not accept that parameter in the installed pygls version, set it via:
```python
server.sync_kind = types.TextDocumentSyncKind.Full
```

Verify against the installed pygls version. The `LanguageServer` constructor accepts `text_document_sync_kind` in pygls 1.x.

#### 3.3.4 `textDocument/didOpen` Handler

```python
@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def on_did_open(params: types.DidOpenTextDocumentParams) -> None:
```

1. Extract `uri = params.text_document.uri`, `version = params.text_document.version`, `text = params.text_document.text`.
2. Call `parser.CACHE.update(uri, version, text)` to parse and cache.
3. Call `_publish_diagnostics(uri, tree)`.

#### 3.3.5 `textDocument/didChange` Handler

```python
@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(params: types.DidChangeTextDocumentParams) -> None:
```

1. Extract `uri = params.text_document.uri`, `version = params.text_document.version`.
2. `text = params.content_changes[0].text` — with `TextDocumentSyncKind.Full` this is always the complete document text. The type of `content_changes[0]` is `TextDocumentContentChangeEvent` which has a `.text` field when sync kind is Full.
3. Call `parser.CACHE.update(uri, version, text)`.
4. Call `_publish_diagnostics(uri, tree)`.

#### 3.3.6 `textDocument/didClose` Handler

```python
@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def on_did_close(params: types.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    parser.CACHE.remove(uri)
```

#### 3.3.7 `_publish_diagnostics` Helper

Extract the repeated diagnostic-publishing logic into a module-level helper (not a `server.feature` handler):

```python
def _publish_diagnostics(uri: str, tree: tree_sitter.Tree) -> None:
    diags: list[types.Diagnostic] = []
    diags.extend(diagnostics.get_parse_diagnostics(tree))
    if _eval_error is not None:
        diags.extend(diagnostics.get_eval_diagnostics(_eval_error, uri))
    server.publish_diagnostics(uri, diags)
```

#### 3.3.8 Updated `on_completion` Handler

The current signature takes `line_to_cursor: str`. This must change to pass the full document tree and position for tree-sitter context detection. The new call site:

```python
items = get_completions(
    evaluator=_evaluator,
    monorepo_root=_monorepo_root,
    current_file=current_file,
    uri=uri,
    position=position,
    cache=parser.CACHE,
    workspace=server.workspace,
)
```

See section 3.4 for the updated `get_completions` signature.

#### 3.3.9 Updated `on_definition` Handler

Replace passing `line_text` and `char` with `uri`, `position`, `cache`, and `workspace`:

```python
return get_definition(
    evaluator=_evaluator,
    monorepo_root=_monorepo_root,
    current_file=current_file,
    uri=uri,
    position=position,
    cache=parser.CACHE,
    workspace=server.workspace,
)
```

See section 3.5 for the updated `get_definition` signature.

#### 3.3.10 `textDocument/hover` Handler

```python
@server.feature(types.TEXT_DOCUMENT_HOVER)
def on_hover(params: types.HoverParams) -> types.Hover | None:
```

**Cache resolution:**
```python
uri = params.text_document.uri
tree = parser.CACHE.get(uri)
if tree is None:
    doc = server.workspace.get_text_document(uri)
    tree = parser.CACHE.update(uri, 0, doc.source)
```

**Node lookup:**
```python
node = parser.node_at_position(tree, params.position.line, params.position.character)
```

**Priority 1 — load path string:**
If the cursor node is a `string` and `find_ancestor(node, "call")` returns a call node where the function text is `"load"` and `node` is the first argument, then:
- Extract the path text (strip surrounding quotes).
- Call `_resolve_load_path(path_text, _monorepo_root, current_file)` from `definition.py`.
- If the path resolves: return `Hover(contents=MarkupContent(kind=MarkupKind.Markdown, value=f"**load path**\n\`{resolved}\`"))`.
- If not: return `Hover(contents=MarkupContent(..., value=f"**load path**\n\`{path_text}\` *(file not found)*"))`.

**Priority 2 — identifier in evaluator globals:**
If the cursor node type is `"identifier"` and `_evaluator is not None`:
```python
raw_path = to_fs_path(uri)
current_file = Path(raw_path) if raw_path else Path(uri)
module_globals = _evaluator._module_globals.get(current_file, {})  # type: ignore[attr-defined]
name = node.text.decode()
if name in module_globals:
    value = repr(module_globals[name])
    return Hover(contents=MarkupContent(
        kind=MarkupKind.Markdown,
        value=f"**{name}**\nValue: \`{value}\`"
    ))
```

**Priority 3 — node type fallback:**
```python
return Hover(contents=MarkupContent(
    kind=MarkupKind.Markdown,
    value=f"**{node.type}**"
))
```

If `node.type` is an empty string (root node on empty document), return `None`.

#### 3.3.11 `textDocument/semanticTokens/full` Handler

```python
TOKEN_TYPES: list[str] = [
    "keyword", "string", "comment", "function",
    "variable", "parameter", "number",
]
TOKEN_MODIFIERS: list[str] = ["definition", "readonly"]

@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=TOKEN_TYPES,
        token_modifiers=TOKEN_MODIFIERS,
    ),
)
def on_semantic_tokens_full(
    params: types.SemanticTokensParams,
) -> types.SemanticTokens:
```

**Tree-sitter to LSP token type mapping:**

| tree-sitter node type | LSP token type | LSP modifier |
|---|---|---|
| `keyword` | `keyword` | — |
| `string` | `string` | — |
| `comment` | `comment` | — |
| `identifier` (child of `function_def`, i.e., function name) | `function` | `definition` |
| `identifier` (left side of `assignment` at module level) | `variable` | `definition`, `readonly` |
| `identifier` (function parameter) | `parameter` | `definition` |
| `integer` | `number` | — |
| `float` | `number` | — |

**Encoding:** The LSP semantic tokens format requires a flat `list[int]` where each token is represented by 5 consecutive integers:
```
[deltaLine, deltaStartChar, length, tokenTypeIndex, tokenModifiersBitfield]
```

`deltaLine` and `deltaStartChar` are relative to the previous token. The first token's deltas are relative to position `(0, 0)`.

`tokenModifiersBitfield` is a bitmask: bit `i` is set if the modifier at index `i` in `TOKEN_MODIFIERS` applies.

Implementation sketch:
```python
tokens: list[tuple[int, int, int, int, int]] = []  # (line, col, len, type, mods)
# ... walk tree, collect (line, col, len, type_idx, mods_bitfield) ...
tokens.sort(key=lambda t: (t[0], t[1]))

data: list[int] = []
prev_line, prev_col = 0, 0
for line, col, length, token_type, mods in tokens:
    data.extend([
        line - prev_line,
        col - prev_col if line == prev_line else col,
        length,
        token_type,
        mods,
    ])
    prev_line, prev_col = line, col

return types.SemanticTokens(data=data)
```

---

### 3.4 `mlody/lsp/completion.py` (MODIFIED)

#### 3.4.1 Signature Change

The public `get_completions` function gains new parameters and loses `line: str`:

```python
def get_completions(
    evaluator: Evaluator | None,
    monorepo_root: Path,
    current_file: Path,
    uri: str,
    position: types.Position,
    cache: parser.DocumentCache,
    workspace: Any,  # pygls Workspace — typed as Any to avoid circular imports
) -> list[CompletionItem]:
```

The `line: str` parameter is removed. The tree-sitter context detection replaces the line-text dependency. `Any` for workspace can be replaced with a Protocol if desired, but is acceptable given NFR-007 allows internal changes.

#### 3.4.2 `_detect_context` Replacement

Delete `_detect_context()` and `_LOAD_RE` entirely. Replace with:

```python
def _detect_context(
    tree: tree_sitter.Tree,
    position: types.Position,
) -> tuple[Literal["load_path", "load_symbol", "builtins_member", "general"], str]:
    """Return (context, partial_text) using tree-sitter AST traversal."""
    node = parser.node_at_position(tree, position.line, position.character)

    # Check if cursor is inside a load() call argument list.
    call_node = parser.find_ancestor(node, "call")
    if call_node is not None:
        func_node = call_node.child_by_field_name("function")
        if func_node is not None and func_node.text == b"load":
            args = call_node.child_by_field_name("arguments")
            if args is not None:
                arg_children = [c for c in args.children if c.type == "string"]
                if arg_children:
                    path_node = arg_children[0]
                    if _node_contains(path_node, position):
                        # Cursor is on the path argument.
                        partial = path_node.text.decode().strip("\"'")
                        return "load_path", partial
                    else:
                        # Cursor is on a symbol argument.
                        return "load_symbol", ""

    # Check for builtins. member access: identifier "builtins" followed by "."
    if node.type == "identifier":
        # Check if the text before cursor ends with "builtins."
        # We still need the raw line text for this one narrow case.
        # Extract from the tree: look for attribute_access ancestor.
        parent = node.parent
        if parent is not None and parent.type == "attribute":
            obj_node = parent.child_by_field_name("object")
            if obj_node is not None and obj_node.text == b"builtins":
                return "builtins_member", ""

    return "general", ""
```

`_node_contains(node, position) -> bool` is a small private helper checking whether the node's start/end range spans the given position.

#### 3.4.3 Updated `_load_path_completions`

Remove the `line: str` parameter. Accept `partial: str` directly (the path text already extracted by `_detect_context`). The rest of the path-resolution logic (resolving `//` vs `:` prefix, listing directory entries) is unchanged.

```python
def _load_path_completions(
    partial: str,
    monorepo_root: Path,
    current_file: Path,
) -> list[str]:
```

#### 3.4.4 `load_symbol` Context

This is a new context not present in the current implementation. When `context == "load_symbol"`, return an empty completion list for now (the symbol names available in the loaded file require reading and evaluating that file, which is deferred). The context is detected and handled without crashing.

#### 3.4.5 Preservation of `_builtin_member_completions` and `_general_completions`

These functions are unchanged. `_builtin_member_completions()` returns `["register", "ctx"]`. `_general_completions(evaluator, current_file)` returns safe builtins plus evaluated module globals.

---

### 3.5 `mlody/lsp/definition.py` (MODIFIED)

#### 3.5.1 Signature Change

```python
def get_definition(
    evaluator: Evaluator | None,
    monorepo_root: Path,
    current_file: Path,
    uri: str,
    position: types.Position,
    cache: parser.DocumentCache,
    workspace: Any,
) -> Location | None:
```

The `line: str` and `char: int` parameters are removed.

#### 3.5.2 Delete Regex-Based Functions

Delete the following functions entirely — no fallback preserved:
- `_extract_load_string(line, char)`
- The `re.finditer` loop at the bottom of `get_definition`

#### 3.5.3 Updated `get_definition` Logic

```python
def get_definition(...) -> Location | None:
    if evaluator is None:
        return None

    # Resolve tree.
    tree = cache.get(uri)
    if tree is None:
        doc = workspace.get_text_document(uri)
        tree = cache.update(uri, 0, doc.source)

    node = parser.node_at_position(tree, position.line, position.character)

    # Mode 1: cursor on load() path string.
    load_stmts = parser.get_load_statements(tree)
    for stmt in load_stmts:
        if _node_contains(stmt.path_node, position):
            target = _resolve_load_path(stmt.path, monorepo_root, current_file)
            return _make_location(target, 0) if target else None

        # Mode 2: cursor on an imported symbol string.
        for sym in stmt.symbols:
            if _node_contains(sym.node, position):
                source_file = _resolve_load_path(stmt.path, monorepo_root, current_file)
                if source_file is None:
                    return None
                def_line = _find_symbol_line(source_file, sym.name)
                return _make_location(source_file, def_line or 0)

    # Mode 3: cursor on a symbol identifier — find it in direct imports.
    symbol = node.text.decode() if node.type == "identifier" else None
    if symbol is None:
        return None
    if symbol in SAFE_BUILTINS:
        return None

    current_globals: dict[str, object] = evaluator._module_globals.get(  # type: ignore[attr-defined]
        current_file, {}
    )
    if symbol not in current_globals:
        return None

    for stmt in load_stmts:
        for sym in stmt.symbols:
            if sym.name == symbol:
                source_file = _resolve_load_path(stmt.path, monorepo_root, current_file)
                if source_file is None:
                    continue
                def_line = _find_symbol_line(source_file, symbol)
                if def_line is not None:
                    return _make_location(source_file, def_line)

    return None
```

`_resolve_load_path`, `_find_symbol_line`, `_make_location`, `_ASSIGNMENT_RE`, `_DEF_RE`, and `_IDENT_RE` are kept unchanged. `_extract_load_string` and `_extract_symbol_at_cursor` are deleted.

A private `_node_contains(node, position) -> bool` helper is needed in both `completion.py` and `definition.py`. To avoid duplication, add it to `parser.py` as a public helper:

```python
def node_contains_position(node: tree_sitter.Node, line: int, character: int) -> bool:
    """Return True if the node's range contains the given LSP position."""
    start_line, start_char = node.start_point
    end_line, end_char = node.end_point
    if line < start_line or line > end_line:
        return False
    if line == start_line and character < start_char:
        return False
    if line == end_line and character > end_char:
        return False
    return True
```

---

## 4. Data Flow

### 4.1 Diagnostics Data Flow

```
Workspace.load() raises exc
  -> _eval_error = exc  (stored in server.py module state)

didOpen / didChange fires
  -> parser.CACHE.update(uri, version, text)  -> tree
  -> diagnostics.get_parse_diagnostics(tree)  -> list[Diagnostic]
  -> diagnostics.get_eval_diagnostics(_eval_error, uri)  -> list[Diagnostic]
  -> server.publish_diagnostics(uri, parse_diags + eval_diags)
```

If `_eval_error is None`, `get_eval_diagnostics` is not called and the eval list is empty.

If both sources produce zero diagnostics, `publish_diagnostics(uri, [])` is still sent — this clears any prior diagnostics the client is displaying.

### 4.2 Hover Priority

For a given cursor position, hover resolution tries three strategies in order and returns the first match:

1. **Load path string:** `node.type == "string"` and `find_ancestor(node, "call")` resolves to a `load()` call and `node` is the first argument.
2. **Identifier in evaluator globals:** `node.type == "identifier"` and `node.text.decode() in _evaluator._module_globals.get(current_file, {})`.
3. **Node type fallback:** Always returns `**{node.type}**` unless the node type is empty.

### 4.3 Evaluator Error Matching

`get_eval_diagnostics` matches the exception to a document by comparing the filesystem paths in the traceback against `to_fs_path(uri)`. This means the diagnostic is only positioned correctly when the error originates in the `.mlody` file being edited. Errors originating in the evaluator runtime code itself (e.g., a bug in `starlarkish`) fall back to line 0, which is acceptable per the requirements.

---

## 5. Dependencies

### 5.1 `pyproject.toml` Changes

Add to the `dependencies` list:

```toml
# Tree-sitter core Python bindings
# See: https://github.com/tree-sitter/py-tree-sitter
"tree-sitter",

# Starlark grammar for tree-sitter
# See: https://github.com/tree-sitter-grammars/tree-sitter-starlark
"tree-sitter-starlark",
```

No version pins. After adding, run `o-repin` to regenerate lock files.

### 5.2 `mlody/lsp/BUILD.bazel` Changes

**Update `lsp_lib`** — add two new source files and two new pip deps:

```starlark
o_py_library(
    name = "lsp_lib",
    srcs = [
        "__init__.py",
        "completion.py",
        "definition.py",
        "diagnostics.py",   # NEW
        "log_handler.py",
        "parser.py",        # NEW
        "server.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "//common/python/starlarkish/evaluator:evaluator_lib",
        "//mlody/core:workspace_lib",
        "@pip//lsprotocol",
        "@pip//pygls",
        "@pip//tree_sitter",         # NEW
        "@pip//tree_sitter_starlark", # NEW
    ],
)
```

**Add `parser_test` target:**

```starlark
o_py_test(
    name = "parser_test",
    srcs = ["parser_test.py"],
    deps = [
        ":lsp_lib",
        "@pip//tree_sitter",
        "@pip//tree_sitter_starlark",
    ],
)
```

**Add `diagnostics_test` target:**

```starlark
o_py_test(
    name = "diagnostics_test",
    srcs = ["diagnostics_test.py"],
    deps = [
        ":lsp_lib",
        "@pip//lsprotocol",
        "@pip//tree_sitter",
        "@pip//tree_sitter_starlark",
    ],
)
```

**Update existing test targets** — `completion_test`, `definition_test`, and `server_test` gain the tree-sitter deps:

```starlark
o_py_test(
    name = "completion_test",
    srcs = ["completion_test.py"],
    deps = [
        ":lsp_lib",
        "@pip//lsprotocol",
        "@pip//pygls",
        "@pip//tree_sitter",
        "@pip//tree_sitter_starlark",
    ],
)
```

Apply the same addition to `definition_test` and `server_test`.

---

## 6. Testing

### 6.1 New Test File: `mlody/lsp/parser_test.py`

```python
"""Tests for mlody.lsp.parser — DocumentCache, grammar loading, query helpers."""
```

**Test class: `TestGrammarLoading`**
- `test_starlark_language_is_valid_language_instance` — assert `isinstance(parser.STARLARK_LANGUAGE, Language)`
- `test_parser_module_import_does_not_raise` — import succeeds (grammar installed)

**Test class: `TestDocumentCache`**
- `test_update_returns_tree` — `cache.update(uri, 1, "x = 1")` returns a `Tree`
- `test_same_version_returns_same_object` — calling `update(uri, 1, ...)` twice returns the same tree object (identity check: `tree1 is tree2`)
- `test_new_version_reparses` — `update(uri, 2, new_text)` after `update(uri, 1, old_text)` returns a different tree object
- `test_get_returns_none_on_miss` — `cache.get("file:///never_opened")` returns `None`
- `test_remove_evicts_entry` — after `update` then `remove`, `cache.get` returns `None`
- `test_update_with_syntax_error_returns_tree_with_has_error` — text `"def ("` produces a tree where `tree.root_node.has_error` is `True`
- `test_multiple_documents_isolated` — two URIs maintain separate cache entries

**Test class: `TestNodeAtPosition`**
- `test_returns_identifier_node_on_identifier` — for `"MY_VAR = 1"`, position `(0, 2)` is inside `MY_VAR` and returns an `identifier` node
- `test_returns_root_for_position_past_end` — position `(999, 0)` returns root node, no crash
- `test_returns_deepest_node` — the returned node has no children containing the position

**Test class: `TestFindAncestor`**
- `test_finds_assignment_ancestor` — for an `identifier` node inside an assignment, `find_ancestor(node, "assignment")` returns the assignment node
- `test_returns_none_when_no_matching_ancestor` — `find_ancestor(node, "nonexistent_type")` returns `None`
- `test_returns_none_for_root_node` — calling with the root node returns `None`

**Test class: `TestGetLoadStatements`**
- `test_single_line_load` — `'load("//a.mlody", "X")'` produces one `LoadStatement` with `path == "//a.mlody"` and `symbols[0].name == "X"`
- `test_multiline_load` — path on line 1, symbols on lines 2 and 3 produces the same `LoadStatement` structure
- `test_multiple_loads` — two `load()` calls produce two `LoadStatement` objects
- `test_no_loads_returns_empty_list` — document with no `load()` returns `[]`
- `test_load_with_multiple_symbols` — `load("//a.mlody", "X", "Y", "Z")` produces three `ImportedSymbol` entries

### 6.2 New Test File: `mlody/lsp/diagnostics_test.py`

```python
"""Tests for mlody.lsp.diagnostics — parse and eval diagnostic generation."""
```

**Test class: `TestGetParseDiagnostics`**
- `test_empty_for_valid_tree` — `"x = 1"` produces `[]`
- `test_one_diagnostic_for_error_node` — `"def ("` produces a list with at least one diagnostic with `severity == DiagnosticSeverity.Error` and `source == "mlody-lsp"`
- `test_diagnostic_range_matches_node_position` — check that `diagnostic.range.start.line` matches the known error line
- `test_missing_node_diagnostic_message_contains_missing` — a tree with a `MISSING` node produces a message containing `"missing"`
- `test_diagnostic_message_format_for_error_node` — message contains `"Syntax error: unexpected"`

**Test class: `TestGetEvalDiagnostics`**
- `test_syntax_error_position` — `SyntaxError` with `lineno=3, offset=5` produces `Position(line=2, character=4)`
- `test_syntax_error_message` — message starts with `"SyntaxError:"`
- `test_name_error_with_traceback` — build a real `NameError` by executing `exec("x = undefined_name", {"__builtins__": {}})` in a context where `co_filename` can be matched; verify line is non-zero and message starts with `"NameError:"`
- `test_exception_without_matching_frame_falls_back_to_line_0` — a `ValueError` with no traceback frame matching `uri` produces `Position(line=0, character=0)`
- `test_source_is_mlody_lsp` — all diagnostics have `source == "mlody-lsp"`
- `test_severity_is_error` — all diagnostics have `severity == DiagnosticSeverity.Error`

### 6.3 Modified Test File: `mlody/lsp/completion_test.py`

**Delete:** `TestDetectContext` class and all its test methods — `_detect_context` with the regex signature is removed.

**Add: `TestDetectContextTreeSitter`** (replaces `TestDetectContext`)
- `test_load_path_detected_multiline` — parse a document with a multi-line `load()` call; create a tree; call the new `_detect_context(tree, position)` with a position inside the path string on line 2; assert `"load_path"` is returned
- `test_load_symbol_detected` — position inside a symbol string argument of a `load()` call returns `"load_symbol"`
- `test_builtins_member_detected` — position on `ctx` in `builtins.ctx` returns `"builtins_member"`
- `test_general_for_identifier` — position on a plain identifier returns `"general"`

**Update: `TestLoadPathCompletions`**
- Change `_load_path_completions(line=..., ...)` calls to `_load_path_completions(partial=..., ...)` matching the new signature
- Existing content tests remain; the `test_bare_prefix_returns_empty` test verifies `partial=""` or a non-`//`/`:` string returns `[]`

**Add: `TestGetCompletionsMultilineLoad`**
- `test_multiline_load_path_completions` — end-to-end test with a real tree-sitter parse: document has a multi-line `load()` call; `get_completions` with position inside the path string returns file-path items
  - This test requires a real `DocumentCache` populated with the document text and a `tmp_path` filesystem fixture

**Keep all existing `TestGetCompletions` tests.** The public `get_completions` API changes signature; update call sites in tests to pass `uri`, `position`, `cache`, `workspace` instead of `line`.

### 6.4 Modified Test File: `mlody/lsp/definition_test.py`

**Delete:** `TestExtractLoadString` class — `_extract_load_string` is deleted.

**Keep:** `TestResolveLoadPath`, `TestFindSymbolLine`, `TestExtractSymbolAtCursor` (the identifier cursor helper is kept for Mode 3).

**Update: `TestGetDefinition`**
- Change all `get_definition(..., line=..., char=...)` calls to `get_definition(..., uri=..., position=..., cache=..., workspace=...)`.
- For `navigates_to_file_on_load_path_cursor`: provide a real `DocumentCache` pre-populated with the document source; set cursor `position` to `Position(line=0, character=10)` (inside the path string).
- Add `test_multiline_load_path_navigation` — multi-line `load()` with path on line 1; cursor at `Position(line=1, character=10)`; expect navigation to the loaded file.
- Add `test_multiline_load_symbol_navigation` — multi-line `load()` with a symbol on line 2; cursor at `Position(line=2, character=5)`; expect navigation to the symbol's definition line.

### 6.5 Modified Test File: `mlody/lsp/server_test.py`

**Add: `TestDidOpenDiagnostics`**
- `test_did_open_publishes_diagnostics_for_error_doc` — mock `parser.CACHE.update` to return a tree where `root_node.has_error` is True, mock `diagnostics.get_parse_diagnostics` to return one diagnostic, assert `server.publish_diagnostics` is called with that diagnostic.
- `test_did_open_clears_diagnostics_for_valid_doc` — mock a tree with no errors; assert `publish_diagnostics` is called with an empty list.

**Add: `TestDidChangeDiagnostics`**
- `test_did_change_publishes_updated_diagnostics` — simulate a `didChange` event; verify cache update and diagnostics publication.

**Add: `TestDidClose`**
- `test_did_close_evicts_cache` — call `on_did_close` with a URI; assert `parser.CACHE.get(uri)` returns `None`.

**Add: `TestHover`**
- `test_hover_load_path_string_resolved` — cursor on a load path string; mock `definition._resolve_load_path` to return a real path; assert hover content contains that path.
- `test_hover_load_path_string_not_found` — mock returns `None`; assert hover content contains `"(file not found)"`.
- `test_hover_identifier_with_eval_value` — cursor on `MY_CONST`; mock `_evaluator._module_globals` to contain the key; assert hover contains `"Value:"`.
- `test_hover_fallback_to_node_type` — cursor on a keyword node; assert hover contains `**keyword**`.
- `test_hover_returns_none_when_evaluator_none` — evaluator is None; cursor on identifier not in a load path; hover still returns the fallback node type (evaluator check only gates priority 2, not priority 3).

---

## 7. Implementation Order

Dependencies between phases determine sequencing. Phases without cross-phase dependencies may proceed in parallel.

| Phase | Work | Depends on | Parallelism |
|---|---|---|---|
| 1 | Add `tree-sitter` and `tree-sitter-starlark` to `pyproject.toml`; run `o-repin`; add new `srcs` and `deps` to `mlody/lsp/BUILD.bazel`; add two new `o_py_test` targets | — | Start immediately |
| 2 | Implement `parser.py` (`STARLARK_LANGUAGE`, `_parser`, `DocumentCache`, dataclasses, `node_at_position`, `find_ancestor`, `node_contains_position`, `get_load_statements`); write `parser_test.py` | Phase 1 | — |
| 3 | Implement `diagnostics.py` (`get_parse_diagnostics`, `get_eval_diagnostics`); write `diagnostics_test.py` | Phase 2 | — |
| 4 | Modify `server.py`: add `_eval_error` state; update `on_initialized` and `on_changed_watched_files` exception handling; add `on_did_open`, `on_did_change`, `on_did_close` handlers; add `_publish_diagnostics` helper; upgrade `TextDocumentSyncKind` to `Full` | Phases 2, 3 | — |
| 5 | Refactor `completion.py`: delete `_detect_context` and `_LOAD_RE`; implement tree-sitter `_detect_context(tree, position)`; update `_load_path_completions` signature; update `get_completions` signature; update `server.py` call site; update `completion_test.py` | Phase 2, 4 | Parallel with Phase 6 |
| 6 | Refactor `definition.py`: delete `_extract_load_string` and regex loop; implement `get_definition` using `get_load_statements`; update signature; update `server.py` call site; update `definition_test.py` | Phase 2, 4 | Parallel with Phase 5 |
| 7 | Implement hover handler in `server.py`; write hover tests in `server_test.py` | Phases 2, 4 | Parallel with Phase 8 |
| 8 | Implement semantic tokens handler in `server.py`; write semantic token tests | Phases 2, 4 | Parallel with Phase 7 |

**Critical path:** 1 -> 2 -> 3 -> 4 -> (5, 6, 7, 8 in parallel)

**Verification after each phase:**
```sh
bazel test //mlody/lsp/...
bazel build --config=lint //mlody/...
```

---

## 8. Non-Functional Requirements Checklist

| NFR | Requirement | Implementation Notes |
|---|---|---|
| NFR-001 | basedpyright strict, no `type: ignore` except documented tree-sitter stub gaps | `tree_sitter.Node` and `tree_sitter.Tree` may lack complete stubs; suppress only with inline comment citing NFR-001 |
| NFR-002 | ruff formatting, no lint violations | Run `bazel build --config=lint //mlody/...` before each PR |
| NFR-003 | `parser_test.py` and `diagnostics_test.py` required | Use `pyfakefs` or `tmp_path` for filesystem tests; `InMemoryFS` from `//common/python/starlarkish/evaluator:testing` if available |
| NFR-004 | Parse only on `didOpen` / `didChange`; handlers use `CACHE.get()` | `DocumentCache.get()` is O(1) dict lookup; no parse in completion/hover/definition/semanticTokens hot paths |
| NFR-005 | No stdout from tree-sitter | tree-sitter Python bindings do not write to stdout; verify with `test_noop_print_produces_no_stdout` style test |
| NFR-006 | Fail fast on missing grammar | Wrap `import tree_sitter_starlark` at module top in try/except; re-raise with actionable message |
| NFR-007 | Preserve `get_completions` and `get_definition` signatures | Signatures change per this spec; `server.py` call sites are updated in Phases 5/6. The public names are preserved; parameter changes are internal refactors |
| NFR-008 | Bazel hermeticity | `@pip//tree_sitter` and `@pip//tree_sitter_starlark` declared in all dependent `deps` lists |

Note on NFR-007: the requirements state "callers in `server.py` must not require modification to their call sites." However, the signature must change to pass `uri`, `position`, `cache`, and `workspace` instead of `line` and `char`. The `server.py` call sites will be updated as part of Phases 5 and 6 — this is explicitly in scope. The requirement is interpreted as: the names `get_completions` and `get_definition` are preserved and the caller (only `server.py`) is updated consistently.

---

## 9. Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
|---|---|---|---|
| `tree_sitter.Node` objects become invalid after tree replacement | Stale node access causes undefined behaviour or crash | Low | Handlers must not store `Node` objects outside request scope. `LoadStatement` and `ImportedSymbol` hold `Node` references — use them and discard within the same handler invocation |
| `TextDocumentSyncKind.Full` increases JSON-RPC payload on every keystroke | Latency increase for very large `.mlody` files | Low | Acceptable for files under 1000 lines. Document as a known limitation; incremental sync is a future optimisation |
| Test fixtures that bypass `didOpen` will trigger eager parse via `workspace.get_text_document()` | Tests fail with `AttributeError` or `KeyError` if `workspace` is not mocked | Medium | Update test setup to either pre-populate `DocumentCache` directly or mock `workspace.get_text_document()` to return a fixture document |
| Semantic token delta encoding bugs produce silent garbled highlighting | Hard to debug; no error from the client | Medium | Write round-trip tests: encode a known token list, decode back, compare. Test with tokens on the same line (delta char) and on different lines (delta line) |
| `tree-sitter-starlark` grammar node type names may differ from assumed names (`call`, `argument_list`, `string`, `identifier`) | `get_load_statements` and `_detect_context` return wrong results | Low | Verify node type names in Phase 2 by parsing a sample document and inspecting `node.type` in a test. Adjust grammar node names to match actual output before proceeding to Phases 5–6 |

---

## 10. Files Changed Summary

| File | Status | Phase |
|---|---|---|
| `pyproject.toml` | Modified — add 2 deps | 1 |
| `mlody/lsp/BUILD.bazel` | Modified — new srcs, deps, test targets | 1 |
| `mlody/lsp/parser.py` | New | 2 |
| `mlody/lsp/parser_test.py` | New | 2 |
| `mlody/lsp/diagnostics.py` | New | 3 |
| `mlody/lsp/diagnostics_test.py` | New | 3 |
| `mlody/lsp/server.py` | Modified | 4, 7, 8 |
| `mlody/lsp/server_test.py` | Modified | 4, 7, 8 |
| `mlody/lsp/completion.py` | Modified | 5 |
| `mlody/lsp/completion_test.py` | Modified | 5 |
| `mlody/lsp/definition.py` | Modified | 6 |
| `mlody/lsp/definition_test.py` | Modified | 6 |

---

## Appendix A: Key tree-sitter API Reference

All APIs are from `tree_sitter` (Python package, version 0.21+):

```python
# Grammar and parser setup
from tree_sitter import Language, Parser, Tree, Node
import tree_sitter_starlark

language = Language(tree_sitter_starlark.language())
p = Parser(language)

# Parsing
tree: Tree = p.parse(b"x = 1")   # bytes required

# Tree properties
tree.root_node              # Node at root
tree.root_node.has_error    # True if any ERROR nodes exist

# Node properties
node.type                   # str, e.g. "identifier", "call", "string", "ERROR"
node.text                   # bytes | None — source text of this node
node.start_point            # tuple[int, int] — (row, column), 0-indexed
node.end_point              # tuple[int, int] — (row, column), 0-indexed
node.is_error               # True for ERROR nodes
node.is_missing             # True for MISSING nodes
node.children               # list[Node] — all children (named and anonymous)
node.named_children         # list[Node] — named children only
node.parent                 # Node | None
node.child_by_field_name(name)  # Node | None — child at named field

# Cursor-based tree walk
cursor = tree.root_node.walk()
cursor.node                 # current Node
cursor.goto_first_child()   # bool
cursor.goto_next_sibling()  # bool
cursor.goto_parent()        # bool
```

Note: `node.text` returns `None` for nodes that do not have source text (e.g., synthetic `MISSING` nodes). Always guard with `if node.text is not None`.

---

## Appendix B: Starlark Grammar Node Types (tree-sitter-starlark)

Verified node type names for the constructs used in this feature. Confirm by running `parser_test.py` in Phase 2.

| Starlark construct | tree-sitter node type |
|---|---|
| Function call `f(...)` | `call` |
| Identifier (name) | `identifier` |
| String literal `"..."` or `'...'` | `string` |
| Integer literal `42` | `integer` |
| Float literal `3.14` | `float` |
| Comment `# ...` | `comment` |
| Assignment `x = 1` | `assignment` |
| Function definition `def f():` | `function_definition` |
| Function parameter | `identifier` (inside `parameters`) |
| Attribute access `a.b` | `attribute` |
| Field name for call function | `function` |
| Field name for call arguments | `arguments` |

Grammar source: `https://github.com/tree-sitter-grammars/tree-sitter-starlark`
