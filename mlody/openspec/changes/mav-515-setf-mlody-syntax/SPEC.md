# SPEC: `.mlody` Surface Syntax for `setf`

**Status:** Draft  
**Depends on:** `mav-515-setf-python`

## Summary

This change adds a source-language assignment form for `.mlody` that lowers to
 the Python `setf` engine. The first version deliberately avoids parser-level
 generalized lvalues and instead introduces an explicit builtin form:

```starlark
setf(base=value_ref, selector=".config.learning_rate", value=0.001)
```

This keeps the semantics aligned with the Python API while avoiding broad
 changes to Starlark assignment parsing.

## Goals

- Make writable path selection available in `.mlody` configuration code.
- Reuse the existing selector grammar and Python engine semantics.
- Preserve transaction-style bulk validation and per-place lineage events.

## Non-Goals

- Parser support for arbitrary lvalue assignment such as `foo.bar = 1`.
- SQL-backed writable selection.
- Compatibility conversion or permission policy.

## Proposed Surface

### Builtin form

```starlark
setf(
    base=my_value,
    selector=".items[::2]",
    value=42,
    mode="inplace",
    reason="normalize even slots",
)
```

### Semantics

- `base` is the root value returned to the caller after assignment.
- `selector` is parsed with the existing traversal grammar.
- `value` is validated using the same type/representation checks as the Python
  API.
- `mode`, `reason`, and future `author` metadata are passed through to
  `mlody.core.setf.setf(...)`.

## Open Questions

- Should `author` default from evaluator context automatically?
- Should `.mlody` support positional syntax after the keyword form stabilizes?

