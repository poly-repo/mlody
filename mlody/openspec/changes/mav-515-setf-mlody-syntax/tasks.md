# Tasks: mav-515-setf-mlody-syntax

- [ ] Add evaluator support for a `setf(...)` builtin that delegates to
      `mlody.core.setf.setf`.
- [ ] Define how `.mlody` evaluation context supplies lineage metadata such as
      `author` and default `reason`.
- [ ] Add integration tests covering direct, bulk, and projected assignment
      forms in `.mlody` source files.
