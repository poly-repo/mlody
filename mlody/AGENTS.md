# Repository Guidelines

## Project Structure & Module Organization
Core Python packages live at the repo root:
- `cli/`: user-facing commands (`mlody` entrypoint, DAG/shell/show commands).
- `core/`: workspace loading, DAG/plan logic, label parsing.
- `common/`: shared runtime utilities and `.mlody` standard files (`action.mlody`, `types.mlody`, etc.).
- `resolver/`, `db/`, `lsp/`: label resolution, evaluations storage, and language-server features.
- `teams/`: team-specific `.mlody` configuration trees.
- `docs/`, `rfc/`, `openspec/`: architecture notes, RFCs, and change specs.

Tests are colocated with modules and use `*_test.py` naming (for example `core/workspace_test.py`).

## Build, Test, and Development Commands
Run from the monorepo root (where `MODULE.bazel` exists).
- `bazel test //mlody/...`: run all mlody tests.
- `bazel test //mlody/core:workspace_test`: run one test target.
- `bazel build --config=lint //mlody/...`: run lint checks.
- `bazel run //mlody/cli:mlody -- --help`: run CLI locally.
- `bazel run //mlody/lsp:lsp_server`: run the LSP server binary.

## Show Action Graph Notes
- The structural/pruned action graph for `show` and `.agraph` is planned in `core/action_graph.py`.
- The action graph is executed in `cli/show_execution.py`, in `execute_show_action_graph(...)`.
- The terminal show-time work (force virtual values, derive display payloads, build previews) lives in `prepare_show_value(...)` in `cli/show_execution.py`.
- Console `show` calls this path from `cli/show.py`; stage/server `show` calls it from `cli/server.py`.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints, and concise docstrings on public functions.
- Naming: `snake_case` for modules/functions, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Bazel: use `o_py_library`, `o_py_binary`, `o_py_test` from `//build/bzl:python.bzl` (not raw `py_*`).
- Keep `.mlody` assets declared in Bazel `data = [...]` when loaded at runtime.

## Testing Guidelines
- Framework: `pytest` through Bazel `o_py_test` targets.
- Use in-memory/fs fixtures (`pyfakefs`, starlarkish helpers) instead of real filesystem writes.
- CLI tests should use `click.testing.CliRunner`.
- Add or update tests in the same package as the changed behavior.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit-style subjects, often with scopes and optional emoji, e.g.:
- `feat(mlody): expand wildcard labels...`
- `fix(bazel): remove dead java_binary debug targets...`

For PRs, include:
- Clear problem + solution summary.
- Linked issue/spec (for example `#457` or `openspec/changes/...`).
- Bazel commands executed (`bazel test ...`, `bazel build --config=lint ...`).
- Screenshots or CLI output snippets when UI/CLI behavior changes.
