# Starlark Deviations

This document records known ways in which the `starlarkish` evaluator deviates
from the
[Starlark language specification](https://github.com/bazelbuild/starlark/blob/master/spec.md).
It is intended to guide future migration toward strict Starlark compliance.

Each entry notes whether the deviation is **intentional** (a deliberate design
choice), **temporary** (acceptable during prototyping, to be removed), or a
**known gap** (something we cannot yet enforce).

---

## `load()` must appear before other code

**Status:** Intentional, partially enforced.

Starlark requires all `load()` statements to appear at the top of a file, before
any other code. The evaluator enforces this with a static AST pre-analysis
(`_validate_loads_at_top`) that inspects top-level statements before `exec()`
runs.

**Known gap:** The pre-analysis only walks `tree.body` (top-level statements). A
`load()` call nested inside a function body, `if` block, `for` loop, or any
other non-top-level scope will **not** be caught before execution. Such calls
will run at evaluation time and may have partial side-effects before the error
surfaces. Detecting nested `load()` calls would require a full AST walk, which
is not currently implemented.

---

## `python.*` escape namespace

**Status:** Temporary (prototyping aid).

A `python` object is injected into every sandbox, exposing Python-specific
builtins that are not part of Starlark:

| `python.` attribute | Python equivalent  |
| ------------------- | ------------------ |
| `python.hasattr`    | `builtins.hasattr` |
| `python.getattr`    | `builtins.getattr` |
| `python.round`      | `builtins.round`   |
| `python.sum`        | `builtins.sum`     |
| `python.Any`        | `typing.Any`       |
| `python.Callable`   | `typing.Callable`  |

Every `python.*` usage is an audit marker. Run `grep python\.` to find all
locations that need attention before migrating to pure Starlark.

---

## `builtins` host-communication object

**Status:** Intentional (host/guest boundary, will evolve).

A `builtins` object is injected into every sandbox. It is not part of Starlark.
It provides:

- `builtins.register(kind, struct)` — registers a named object with the host
  evaluator.
- `builtins.ctx.directory` — the directory of the currently executing file.

---

## `load()` path forms

**Status:** Intentional, follows Bazel label conventions.

The four supported path forms all follow standard Bazel label syntax
(`@repo//pkg:target`, `//pkg:target`, `:target`, relative). The only real
deviation from standard Starlark `load()` is that labels here resolve to
`.mlody` source files directly rather than to Bazel build targets.

---

## No immutability enforcement

**Status:** Known gap.

Starlark variables are immutable after assignment; reassignment is a compile
error. Python variables are mutable. The evaluator does not enforce Starlark
immutability semantics. This means `.mlody` scripts can reassign top-level names
without error, which would be invalid in real Starlark.

---

## Functions are full Python closures

**Status:** Known gap.

Starlark functions cannot close over mutable state. Python functions can. A
`.mlody` function defined with `def` has full Python closure semantics, which
may allow patterns that are invalid in real Starlark.

---

## `Struct` class exposed as a type

**Status:** Intentional.

The `Struct` class itself (not just `struct()`) is exposed in the sandbox,
allowing `isinstance(obj, Struct)` checks. This is not part of standard
Starlark.
