# mlody/lsp

Python LSP server for `.mlody` files, built on pygls + lsprotocol + tree-sitter-starlark.

## Key Library Findings

### tree-sitter (Python bindings)

- `node.type == "ERROR"` — identifies error-recovery nodes (always available).
- `node.is_missing: bool` — True for parser-inserted placeholder nodes. **Available in the installed version.**
- `node.has_error: bool` — True if the node or any descendant contains an error.
- `node.start_point`, `node.end_point` — `tree_sitter.Point` objects (namedtuple-like, `row`/`column` attributes), zero-indexed. Compare with `==` — they compare equal to each other correctly.

### tree-sitter-starlark — MISSING node behaviour

tree-sitter-starlark uses ERROR recovery rather than MISSING nodes in most cases.
Not all "obviously missing" tokens produce `is_missing=True` nodes:

| Input | Result |
|---|---|
| `load(` | One ERROR node spanning the whole input — **no MISSING node** |
| `def (((` | One ERROR node spanning the whole input — **no MISSING node** |
| `x = @` | Nested ERROR nodes (parent + one per bad char) — **no MISSING node** |
| `if :` | `is_missing=True` on the condition `identifier` — **MISSING node present** |

**Practical consequence:** When writing tests for MISSING-node diagnostics, use `"if :"` (missing `if`-condition) as the triggering input. Do NOT use `"load("` — it produces only an ERROR node.

To produce **multiple** ERROR nodes in one document, intersperse valid code with invalid characters: `"x = @\ny = $"` yields a parent ERROR plus two nested ERROR nodes (one per `@`/`$`).

### lsprotocol

Content-change event types for `DidChangeTextDocumentParams.content_changes`:

| Sync kind | Correct type name |
|---|---|
| Incremental (with range) | `types.TextDocumentContentChangePartial` |
| Full (whole document) | `types.TextDocumentContentChangeWholeDocument` |

The names `TextDocumentContentChangeEvent_Type1` / `_Type2` do **not** exist in the installed version.

### tree-sitter-starlark — AST structure for semantic tokens

**`function_definition` node child order** (verified against tree-sitter-starlark 1.3.0):

> ⚠️ The node type is `"function_definition"` — **not** `"function_def"`. Earlier docs and the design.md were wrong; confirmed by runtime inspection.

```
(function_definition
  "def"          # children[0] — keyword leaf, type == "def"
  (identifier)   # children[1] — function name
  (parameters)   # children[2] — parameter list
  ":"            # children[3]
  (block ...))   # children[4]
```

To detect the function name identifier: `node.parent.type == "function_definition"` and `node.start_point == node.parent.children[1].start_point`.

**`parameters` node children** — simple positional parameters are direct `identifier` children of `parameters`. Non-simple variants have distinct parent node types:

| Syntax | Parent node type |
|---|---|
| `x` (positional) | `parameters` — direct `identifier` child |
| `x=1` (default) | `default_parameter` |
| `*args` | `list_splat_pattern` |
| `**kwargs` | `dictionary_splat_pattern` |

To detect simple parameters: `node.parent.type == "parameters"` (no grandparent check needed).

**Identifier classification priority chain** (must be evaluated in this order inside `_collect_tokens`):

1. `parent.type == "function_definition"` and matches `children[1]` → `function` + `definition`
2. `parent.type == "parameters"` → `parameter` + `definition`
3. `parent.type == "assignment"` and matches `children[0]` → `variable` + `definition` + `readonly`
4. Fallback → `variable` + no modifiers

### pygls (v2)

- LanguageServer constructor: `text_document_sync_kind=` (not `text_document_sync=`).
- Publish diagnostics: `server.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=..., diagnostics=...))`.
  The old `server.publish_diagnostics(uri, diagnostics)` signature is v1 only.

# Deployment

The server can be deployed with:
```shell
bazel build mlody/lsp:lsp_server_pex
chmod 755 bazel-bin/mlody/lsp/lsp_server_pex.pex
cp bazel-bin/mlody/lsp/lsp_server_pex.pex ~/.local/bin/mlody-lsp
```
