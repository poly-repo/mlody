# Requirements Document: Mlody Flexible Workspace Resolution

**Version:** 1.0 **Date:** 2026-03-07 **Prepared by:** Requirements Analyst AI
**Status:** Draft

---

## 1. Executive Summary

Mlody currently constructs its `Workspace` from a single fixed root: the current
working directory at the time the CLI is invoked. This root is assumed to be the
monorepo root and is immutable for the lifetime of the process. This is
sufficient for everyday pipeline authoring, but it prevents users from comparing
target values across commits, reproducing historical results, or evaluating the
same pipeline DAG against a specific release or feature branch.

The proposed feature introduces **flexible workspace resolution**: a label
parsing and committoid-to-workspace mapping layer that sits in front of the
existing `Workspace` constructor. When a target label starts with `@` or `//`,
behaviour is unchanged and the cwd is used as before. When a label begins with
anything else, the prefix up to a `|` delimiter is treated as a _committoid_ (a
commit SHA, branch name, or tag name); mlody resolves this to a canonical full
SHA via the remote, retrieves the source tree for that commit (using a local
clone if the commit is already present locally, otherwise a shallow clone from
`origin`), caches the result under `~/.cache/mlody/workspaces/<SHA>/`, and
constructs the `Workspace` from that cached root.

The expected business value is: reproducible pipeline inspection across time,
faster iteration during development by targeting specific historical states, and
a foundation for the future mixed-version graph caching use case.

---

## 2. Project Scope

### 2.1 In Scope

- Parsing of the extended label syntax (`committoid|path`) in the `show`
  command.
- Committoid resolution via `git ls-remote origin` for all input types (SHA,
  branch name, tag name), with ambiguity detection.
- Post-resolution local existence check (`git cat-file -t`) used solely to
  choose the clone strategy (local vs remote).
- Workspace materialisation: local shallow clone or origin shallow clone into
  `~/.cache/mlody/workspaces/<SHA>/`.
- Cache hit detection: reuse an already-materialised workspace directory without
  network access.
- Metadata recording: a `<SHA>-meta.json` file alongside each cached workspace
  directory.
- File-based locking to guard against concurrent materialisations of the same
  SHA.
- Error handling for: network failures, unknown refs, ambiguous short SHAs,
  corrupt/partial cache entries, and lock contention.
- A `resolve_workspace()` factory function that returns a ready `Workspace`
  instance.
- `show` and `shell` commands (the latter inherits resolution through `show`).

### 2.2 Out of Scope

- Mixed-version graphs (different nodes resolving against different commits in a
  single invocation) — deferred to a future phase.
- Task output caching (memoising the result of executing a pipeline node).
- Cache eviction, size limits, or automated cleanup.
- Making cached workspaces read-only (Bazel must be able to write output
  directories inside them).
- Offline mode (network access is assumed whenever a committoid is specified).
- Support for git remotes other than `origin`.
- Container image building (shares the workspace caching mechanism but is not
  part of this feature's CLI surface).
- Any changes to the `Workspace` class constructor or internals.
- UI for browsing or managing cached workspaces beyond the metadata file.

### 2.3 Assumptions

- The mlody CLI is always invoked from within a valid monorepo root (enforced by
  the existing `verify_monorepo_root()` check).
- The `git` CLI is installed and authenticated for the remote `origin`; ambient
  git credentials are available.
- The `gh` CLI is installed and authenticated; this ensures credentials are
  available for private repo access without mlody managing tokens.
- The remote is always named `origin`; multi-remote support is not required.
- Single-process usage: no two mlody processes will routinely race on the same
  SHA. The lock file is a safety net, not a performance mechanism.
- Cached workspace entries addressed by full commit SHA are immutable. A given
  SHA always produces the same source tree.
- The user is always present and interactive when running mlody in experiment
  mode; unattended background execution is not required.
- The cwd may itself be a git worktree rather than the main clone; the feature
  must handle this transparently.

### 2.4 Constraints

- Python 3.13.2, hermetic via rules_python.
- Type checking: basedpyright strict mode; all new function signatures must
  carry complete type hints.
- Formatting/linting: ruff.
- Build rules: `o_py_library`, `o_py_binary`, `o_py_test` from
  `//build/bzl:python.bzl`; no raw `py_*` rules.
- Cache directory: `~/.cache/mlody/workspaces/` (XDG cache convention).
- The `Workspace` class (`mlody/core/workspace.py`) must not be modified as part
  of this feature.

---

## 3. Stakeholders

| Role                | Name/Group          | Responsibilities                            |
| ------------------- | ------------------- | ------------------------------------------- |
| Primary user        | ML engineers / devs | Run `mlody show` against historical commits |
| Feature author      | Polymath Solutions  | Design, implement, review                   |
| Future stakeholders | Platform / infra    | Container image build pipeline (future use) |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Enable engineers to inspect and compare mlody pipeline target
  values at any commit, branch, or tag without manually checking out that ref.
- **BR-002:** Enable reproducible pipeline inspection: given the same
  committoid, mlody must resolve to the same full SHA and source tree every
  time.
- **BR-003:** Avoid redundant network operations: once a workspace is cached for
  a given SHA, subsequent invocations must reuse it without re-fetching.
- **BR-004:** Lay the groundwork for the future mixed-version graph caching use
  case by establishing a stable, SHA-keyed workspace cache.

### 4.2 Success Metrics

- **KPI-001:** `mlody show <branch>|<label>` resolves and displays the correct
  value; target: passes integration test against a known fixture commit.
- **KPI-002:** A second invocation with the same committoid completes without
  any `git clone` subprocess (cache hit); measurement: mock or subprocess
  capture in tests.
- **KPI-003:** An invalid committoid produces a human-readable error message
  within 5 seconds; measurement: manual test.
- **KPI-004:** Lock contention produces an immediate, informative error rather
  than a hang; measurement: unit test with a pre-placed lock file.

---

## 5. User Requirements

### 5.1 User Personas

**Persona 1: ML Engineer (primary)**

- Goals: quickly inspect a target value as it was on `main` last week; compare
  config between two branches; reproduce a result from a tagged release.
- Pain points: currently must `git stash`, `git checkout`, re-run mlody, then
  restore — slow and error-prone.
- Needs: a single command that handles all of that transparently.

### 5.2 User Stories

**Epic 1: Historical Target Inspection**

- **US-001:** As an ML engineer, I want to run
  `mlody show main|@lexica//models:bert` so that I can see the value of that
  target at the current tip of `main` without checking out that branch.
  - Acceptance Criteria: Given I am at the monorepo root on any branch, when I
    run the command, then mlody resolves `main` to a full SHA, materialises or
    reuses the workspace for that SHA, and prints the resolved value.
  - Priority: Must Have

- **US-002:** As an ML engineer, I want to run
  `mlody show v1.2.0|@lexica//models:bert` so that I can inspect the value at a
  tagged release.
  - Acceptance Criteria: Given a valid tag `v1.2.0` exists on `origin`, when I
    run the command, then the tag is resolved to its full commit SHA and the
    workspace for that SHA is used.
  - Priority: Must Have

- **US-003:** As an ML engineer, I want to run
  `mlody show abc1234|@lexica//models:bert` so that I can inspect a specific
  commit by short SHA.
  - Acceptance Criteria: Given `abc1234` unambiguously identifies a single
    commit on `origin`, when I run the command, then the full SHA is resolved
    via the remote and the correct workspace is used.
  - Priority: Must Have

- **US-004:** As an ML engineer, I want unchanged behaviour for
  `mlody show @lexica//models:bert` so that existing scripts and habits are not
  broken.
  - Acceptance Criteria: Given a label starting with `@` or `//`, when I run
    `mlody show`, then cwd is used as the workspace root identically to today.
  - Priority: Must Have

- **US-005:** As an ML engineer, I want a clear error message when I mistype a
  branch or tag name so that I know immediately what went wrong.
  - Acceptance Criteria: Given a committoid that does not resolve to any known
    ref on `origin`, when I run `mlody show`, then mlody exits 1 and prints a
    message identifying the unknown ref.
  - Priority: Must Have

- **US-006:** As an ML engineer, I want a clear error when a short SHA is
  ambiguous so that I know to provide a longer SHA.
  - Acceptance Criteria: Given a short SHA that matches multiple commits on
    `origin`, when I run `mlody show`, then mlody exits 1 and tells me the input
    was ambiguous.
  - Priority: Must Have

---

## 6. Functional Requirements

### 6.1 Label Parsing

**FR-001: Committoid prefix detection**

- Description: When a target label is passed to `show`, inspect it before
  passing it to `Workspace.resolve()`. If the label starts with `@` or `//`,
  treat cwd as the workspace root (unchanged behaviour). Otherwise, split on the
  first `|` character to extract the committoid and the inner label.
- Inputs: Raw target string from CLI argument.
- Processing: String prefix check (`@`, `//`); `str.split('|', maxsplit=1)` if
  neither prefix matches.
- Outputs: `(committoid: str | None, inner_label: str)` pair.
- Business Rules:
  - If `|` is absent and the label does not start with `@` or `//`, this is a
    parse error — emit a clear message and exit 1.
  - The inner label (the part after `|`) must itself start with `@` or `//`; if
    it does not, emit a parse error.
  - When multiple targets are passed to a single `mlody show` invocation and
    they resolve to different committoids, the behaviour is [TBD — error for
    now].
- Priority: Must Have
- Dependencies: None.

### 6.2 Committoid Resolution

**FR-002: Remote resolution via `git ls-remote`**

- Description: For all committoid inputs — whether they look like a SHA or a ref
  name — resolve to a single, unambiguous full SHA by querying `origin` via
  `git ls-remote`. This is the canonical resolution step for all input types.
- Inputs: Committoid string (hex SHA, branch name, or tag name).
- Processing:
  1. Run `git ls-remote origin` (or `git ls-remote origin <committoid>` for
     efficiency) against the local repo's `origin`.
  2. Parse stdout to find matching refs:
     - For branch-like input: match `refs/heads/<committoid>`.
     - For tag-like input: match `refs/tags/<committoid>`; if the tag is
       annotated, prefer the dereferenced entry (`^{}` suffix) to obtain the
       commit SHA.
     - For hex-string input: match any ref whose SHA starts with the provided
       prefix.
  3. If exactly one commit SHA is identified, use it.
  4. If zero SHAs match, emit an "unknown ref" error and exit 1.
  5. If multiple distinct SHAs match (ambiguous short SHA), emit an "ambiguous
     ref" error and exit 1.
- Outputs: Full 40-character SHA string.
- Business Rules:
  - Ambiguity detection applies to all input types, including hex strings.
  - If `git ls-remote` fails due to a network or authentication error, emit a
    clear error message and exit 1. No retry or fallback.
  - The `--roots` global option is unaffected by this step.
- Priority: Must Have
- Dependencies: `git` CLI on `PATH`; network access to `origin`.

**FR-003: Local existence check (clone strategy hint only)**

- Description: After FR-002 has produced a canonical full SHA, check whether
  that exact commit exists in the local cwd monorepo to determine the optimal
  clone strategy. This check does NOT gate or replace ambiguity validation.
- Inputs: Full 40-character SHA (from FR-002); path to cwd monorepo root.
- Processing: Run `git cat-file -t <full-sha>` in the local repo. If it exits 0
  and outputs `commit`, the commit is available locally.
- Outputs: Boolean `local: bool`.
- Business Rules:
  - This step runs only after FR-002 has produced an unambiguous full SHA.
  - The result is used exclusively to select between FR-004 (local clone) and
    FR-005 (remote clone).
  - A `False` result is not an error.
- Priority: Must Have
- Dependencies: FR-002 (full SHA already known); `git` CLI.

### 6.3 Workspace Materialisation

**FR-004: Cache hit detection**

- Description: Before cloning, check whether a workspace for the resolved SHA
  already exists and is complete.
- Inputs: Full SHA; cache root `~/.cache/mlody/workspaces/`.
- Processing: Check that `~/.cache/mlody/workspaces/<SHA>/` exists and contains
  `mlody/roots.mlody` (the sentinel file indicating a complete workspace). A
  directory that exists but lacks the sentinel is treated as corrupt or partial.
- Outputs: `True` (cache hit — proceed to `Workspace` construction) or `False`
  (materialisation required).
- Business Rules:
  - On cache hit, no clone or network call is made.
  - A corrupt/partial directory (exists but lacks sentinel) is a fatal error;
    emit a message naming the directory and instructing the user to delete it,
    then exit 1. Do not silently re-clone.
- Priority: Must Have
- Dependencies: FR-002 (SHA known).

**FR-005: Local shallow clone**

- Description: If FR-003 found the commit locally and FR-004 found no cache hit,
  clone from the local repo to avoid network access.
- Inputs: Full SHA; path to cwd monorepo root; destination
  `~/.cache/mlody/workspaces/<SHA>/`.
- Processing: Clone from `file:///path/to/monorepo` with `--local --depth 1`,
  then checkout the target SHA. The exact git invocation is an implementation
  detail; the requirement is that no network call is made.
- Outputs: Populated workspace directory at the destination path.
- Business Rules:
  - Runs only when FR-003 returned `local=True` and FR-004 returned `False`.
  - If the clone fails for any reason, clean up the partial destination
    directory and exit 1 with a clear error message.
  - Must be preceded by lock acquisition (FR-006).
- Priority: Must Have
- Dependencies: FR-003 (local=True); FR-004 (cache miss); FR-006 (lock held).

**FR-006: Origin shallow clone**

- Description: If the commit is not in the local repo (FR-003 returned
  `local=False`) and no cache hit exists, perform a shallow clone from `origin`.
- Inputs: Full SHA; destination path.
- Processing: Run a depth-1 clone from `origin` targeting the resolved SHA. The
  exact flags (`--filter=blob:none`, `--no-checkout`, etc.) are an
  implementation detail.
- Outputs: Populated workspace directory at the destination path.
- Business Rules:
  - If the network is unreachable or `origin` rejects the connection, clean up
    the partial directory and exit 1 with a clear network error message.
  - Must be preceded by lock acquisition (FR-007).
- Priority: Must Have
- Dependencies: FR-003 (local=False); FR-004 (cache miss); FR-007 (lock held).

**FR-007: File-based lock for cache materialisation**

- Description: Guard against two concurrent mlody processes attempting to
  materialise the same SHA simultaneously.
- Inputs: Full SHA; cache root.
- Processing: Before starting any clone, attempt to create
  `~/.cache/mlody/workspaces/<SHA>.lock` using an atomic, exclusive,
  non-blocking file-creation operation (e.g. `O_CREAT | O_EXCL`). If the lock
  file already exists, do not wait — emit an informative error immediately and
  exit 1.
- Outputs: Lock file present for the duration of materialisation; removed in a
  `finally` block on success or failure.
- Business Rules:
  - Lock acquisition must be atomic.
  - The error message on contention must name the lock file path so the user can
    inspect or delete it manually if a previous process crashed.
  - Lock is checked only on cache miss (after FR-004).
- Priority: Must Have
- Dependencies: FR-004 (cache miss confirmed).

### 6.4 Metadata Recording

**FR-008: Workspace metadata file**

- Description: After successful materialisation, write a JSON metadata file
  alongside the workspace directory.
- Inputs: Original committoid string as typed by the user; resolved full SHA;
  current UTC timestamp; remote URL of `origin`.
- Processing: Write `~/.cache/mlody/workspaces/<SHA>-meta.json` with the
  following schema:

  ```json
  {
    "requested_ref": "<original committoid as typed by user>",
    "resolved_sha": "<full 40-character SHA>",
    "resolved_at": "<ISO 8601 UTC timestamp>",
    "repo": "<remote URL of origin>"
  }
  ```

- Business Rules:
  - Write after the workspace directory is fully populated and before the lock
    is released.
  - If the metadata file already exists (e.g. from a previous materialisation),
    do not overwrite it.
  - Use the standard library `json` module; the output must be valid JSON.
- Priority: Must Have
- Dependencies: FR-005 or FR-006 (workspace fully materialised).

### 6.5 Factory Function

**FR-009: `resolve_workspace()` factory function**

- Description: A single public entry point that accepts a raw label string and
  returns a loaded `Workspace` instance, encapsulating all resolution and
  materialisation steps.
- Indicative signature:

  ```python
  def resolve_workspace(
      label: str,
      monorepo_root: Path,
      roots_file: Path | None = None,
      print_fn: Callable[..., None] = print,
  ) -> tuple[Workspace, str | None]:
      ...
  ```

  Returns the `Workspace` and the resolved SHA (or `None` if cwd was used, i.e.
  no committoid was present).

- Processing: Orchestrates FR-001 → FR-002 → FR-003 → FR-004 → FR-007 → FR-005
  or FR-006 → FR-008 → `Workspace(monorepo_root=<root>, roots_file=roots_file)`
  → `workspace.load()`.
- Business Rules:
  - Must not modify the `Workspace` class.
  - Must call `workspace.load()` before returning so the caller receives a
    ready-to-use instance.
  - All error conditions must either raise typed exceptions or call
    `sys.exit(1)` with a clear stderr message; the function must never return a
    partially-initialised `Workspace`.
  - When the cwd path is taken (no committoid), `monorepo_root` is passed
    directly to `Workspace` as today.
- Priority: Must Have
- Dependencies: FR-001 through FR-008.

### 6.6 CLI Integration

**FR-010: `show` command uses `resolve_workspace()`**

- Description: The `show` command replaces the direct `Workspace` construction
  in `mlody/cli/main.py` with a call to `resolve_workspace()`.
- Business Rules:
  - The `--roots` global option must continue to work; it is passed through to
    `Workspace` unchanged.
  - When multiple targets are passed to a single `mlody show` invocation and
    they carry different committoids, the behaviour is [TBD — error for now].
  - Verbose logging (`--verbose`) should emit the resolved SHA when a committoid
    path is taken, so users can confirm which commit was used.
- Priority: Must Have
- Dependencies: FR-009.

---

## 7. Non-Functional Requirements

### 7.1 Performance Requirements

- **NFR-001:** Cache hit path (workspace directory already exists and is valid):
  `mlody show` must add no more than 200 ms of overhead compared to today's cwd
  path (no clone, no network call beyond the `git ls-remote` resolution step).
- **NFR-002:** `git ls-remote` latency is bounded by network conditions to
  `origin`; no artificial timeout is imposed beyond the git default.

### 7.2 Scalability Requirements

- **NFR-003:** The cache has no enforced size limit in this version. Each cached
  workspace is a shallow clone, expected to be tens to hundreds of megabytes for
  this monorepo.

### 7.3 Availability & Reliability

- **NFR-004:** On a cache hit, the feature must not make any network calls
  beyond `git ls-remote` resolution. Cloning must not be re-attempted.
- **NFR-005:** A failed materialisation must not leave a corrupt entry in the
  cache. Partial directories and lock files must be cleaned up on failure via
  `finally` blocks.

### 7.4 Security Requirements

- **NFR-006:** No credentials are stored by mlody. Authentication relies
  entirely on the ambient git credential store and `gh` authentication.
- **NFR-007:** The cache directory (`~/.cache/mlody/`) is created with user-only
  permissions (mode `0700`) if it does not already exist.
- **NFR-008:** Committoid inputs are passed to `git` as arguments, never
  interpolated into shell strings, to prevent injection.

### 7.5 Usability Requirements

- **NFR-009:** All error messages must identify: what was attempted, what went
  wrong, and (where applicable) how to recover.
- **NFR-010:** The resolved SHA is emitted to the verbose log (`--verbose`) so
  users can confirm which commit was actually used. Exact UX for non-verbose
  surfacing is deferred pending design of the attributes system.

### 7.6 Maintainability Requirements

- **NFR-011:** Resolution and materialisation logic must be isolated from the
  `Workspace` class and independently testable via `resolve_workspace()`.
- **NFR-012:** All new modules must have corresponding `_test.py` files
  covering: label parsing, committoid resolution (local hit, remote hit, unknown
  ref, ambiguous SHA), cache hit/miss branching, lock contention, metadata
  writing, and each error condition.

### 7.7 Compatibility Requirements

- **NFR-013:** Existing behaviour for labels starting with `@` or `//` must be
  byte-for-byte identical to today — no regressions in existing tests.
- **NFR-014:** The feature must work when cwd is a git worktree rather than the
  main clone.

---

## 8. Data Requirements

### 8.1 Data Entities

- **Cached workspace:** Directory `~/.cache/mlody/workspaces/<SHA>/` containing
  a shallow clone of the monorepo at the given commit.
- **Workspace metadata:** JSON file `~/.cache/mlody/workspaces/<SHA>-meta.json`
  recording provenance of the cached workspace.
- **Lock file:** Temporary file `~/.cache/mlody/workspaces/<SHA>.lock`, present
  only during active materialisation.

### 8.2 Data Quality Requirements

- A workspace directory is considered valid only if `mlody/roots.mlody` exists
  inside it (sentinel check). Any directory failing this check is treated as
  corrupt; the user is instructed to delete it manually.

### 8.3 Data Retention & Archival

- No automated retention policy in this version. Cache entries persist until
  manually deleted. Cache cleanup is deferred to a future feature.

### 8.4 Data Privacy & Compliance

- Cached workspaces contain monorepo source code and must reside in the user's
  home directory under a user-only-readable path, not in shared or
  world-readable locations.

---

## 9. Integration Requirements

### 9.1 External Systems

| System          | Purpose                                                      | Auth           | Error handling                        |
| --------------- | ------------------------------------------------------------ | -------------- | ------------------------------------- |
| `git` CLI       | Ref resolution (`ls-remote`), cloning, local existence check | Ambient creds  | Propagate stderr; exit 1              |
| `origin` remote | Remote ref resolution and clone source                       | git creds / gh | Network error → clear message, exit 1 |

### 9.2 API Requirements

- No HTTP APIs are called directly. All remote operations go through the `git`
  CLI using ambient credentials.

---

## 10. User Interface Requirements

### 10.1 CLI Syntax

Extended label syntax for the `show` command:

```
mlody show [OPTIONS] TARGETS...
```

Where each `TARGET` follows one of these forms:

| Form                            | Meaning                                     |
| ------------------------------- | ------------------------------------------- |
| `@ROOT//pkg:name`               | Current cwd workspace (unchanged behaviour) |
| `//pkg:name`                    | Current cwd workspace (unchanged behaviour) |
| `<committoid>\|@ROOT//pkg:name` | Workspace at resolved committoid            |
| `<committoid>\|//pkg:name`      | Workspace at resolved committoid            |

Valid committoid examples: `main`, `v1.2.0`, `abc1234`, `a1b2c3d4e5f6...` (full
SHA).

### 10.2 Error Message Standards

All error messages emitted to stderr follow the pattern:

```
Error: <what failed>. <reason>. <remediation if applicable>.
```

Examples:

```
Error: Cannot resolve ref 'mian'. No matching branch or tag found on origin.

Error: Ambiguous short SHA 'abc12'. Multiple commits match on origin.
       Provide a longer SHA prefix or the full 40-character SHA.

Error: Cache lock busy for SHA abc123...
       Another mlody process may be materialising this workspace.
       Delete ~/.cache/mlody/workspaces/abc123....lock if the process has exited.

Error: Corrupt workspace cache for SHA abc123...
       Expected mlody/roots.mlody not found inside the workspace directory.
       Delete ~/.cache/mlody/workspaces/abc123.../ and retry.
```

---

## 11. Reporting & Analytics Requirements

Not applicable for this feature. The resolved SHA is surfaced via `--verbose`
logging. Detailed UX for inspecting workspace provenance is deferred pending
design of the attributes system.

---

## 12. Security & Compliance Requirements

### 12.1 Authentication & Authorization

- mlody delegates all authentication to the `git` CLI credential store. No token
  management is introduced.

### 12.2 Data Security

- Cache directory created with mode `0700`.
- No credentials written to disk by mlody.
- Committoid inputs passed as git arguments, not shell-interpolated.

### 12.3 Compliance

- No regulatory compliance requirements identified for this feature.

---

## 13. Infrastructure & Deployment Requirements

### 13.1 Cache Directory Layout

```
~/.cache/mlody/
└── workspaces/
    ├── <full-sha>/               # shallow clone of monorepo at that SHA
    ├── <full-sha>-meta.json      # provenance metadata (written after clone)
    ├── <full-sha>.lock           # present only during active materialisation
    └── ...
```

### 13.2 Deployment

- No new binaries or services. The feature is a library addition within the
  existing `mlody` Python package, surfaced through the existing CLI entry
  point.

### 13.3 Disaster Recovery

- If the cache is lost or corrupted, the next invocation re-materialises from
  `origin`. No persistent user state is lost.

---

## 14. Testing & Quality Assurance Requirements

### 14.1 Testing Scope

| Test type                   | Coverage                                                                                  |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| Unit: label parser          | All valid forms, missing `\|`, invalid inner label, empty input                           |
| Unit: committoid resolver   | Branch hit, tag hit, annotated tag deref, unknown ref, ambiguous short SHA, network error |
| Unit: local existence check | Commit present, commit absent, fallback to remote clone                                   |
| Unit: cache detection       | Hit with valid sentinel, miss, corrupt entry (dir exists, sentinel absent)                |
| Unit: lock logic            | Successful acquire, contention → immediate error, cleanup in finally                      |
| Unit: metadata writer       | Schema correctness, no-overwrite on existing file                                         |
| Integration                 | End-to-end `mlody show <branch>\|<label>` against a local fixture repo                    |
| Regression                  | All existing `show` and `shell` tests pass unchanged                                      |

### 14.2 Acceptance Criteria

- All unit tests pass under `bazel test //mlody/...`.
- `mlody show @lexica//models:bert` (cwd form) is behaviourally identical to
  today's behaviour, verified by existing tests.
- `mlody show main|@lexica//models:bert` resolves and displays the correct value
  against a test fixture commit.
- A repeated invocation of the above produces no `git clone` subprocess (cache
  hit).
- An ambiguous short SHA input exits 1 with an "ambiguous" error message.
- An unknown ref input exits 1 with an "unknown ref" error message.
- A pre-placed lock file causes an immediate exit 1 with a message naming the
  lock file path.

---

## 15. Training & Documentation Requirements

### 15.1 User Documentation

- Update `mlody show --help` to document the extended label syntax with
  examples.
- Add a note to `mlody shell --help` that workspace resolution applies via the
  `show` subcommand.

### 15.2 Technical Documentation

- Module-level docstring in the new resolver module describing the resolution
  algorithm, the two-step (remote-resolve then local-check) approach, and the
  cache layout.
- Docstrings on `resolve_workspace()` and all supporting public functions.

---

## 16. Risks & Mitigation Strategies

| Risk ID | Description                                                     | Impact | Probability | Mitigation                                                          | Owner |
| ------- | --------------------------------------------------------------- | ------ | ----------- | ------------------------------------------------------------------- | ----- |
| R-001   | Stale lock file from a crashed process blocks future runs       | High   | Low         | Error message names the lock path; user can delete manually         | Dev   |
| R-002   | Shallow clone missing objects needed by Bazel rules at that SHA | Medium | Low         | Document limitation; deepen clone on demand in a future phase       | Dev   |
| R-003   | `git ls-remote` slow on large repos or slow networks            | Low    | Medium      | Acceptable for experimental use; optimise in a future phase         | Dev   |
| R-004   | cwd is a git worktree; `git clone --local` behaves differently  | Medium | Medium      | Explicitly test against worktree cwd; adjust clone source as needed | Dev   |
| R-005   | Cache grows unboundedly in long-running dev environments        | Medium | Medium      | Accepted for now; cleanup deferred to a future feature              | Dev   |
| R-006   | Both a branch and a tag share the same name, causing ambiguity  | Low    | Low         | Document preference order (branch > tag); expose in error message   | Dev   |

---

## 17. Dependencies

| Dependency                           | Type     | Status  | Impact if Delayed                      | Owner   |
| ------------------------------------ | -------- | ------- | -------------------------------------- | ------- |
| `git` CLI on PATH                    | External | Met     | Blocks all resolution and clone logic  | Env     |
| `gh` CLI (ambient auth)              | External | Met     | Blocks private repo access             | Env     |
| Existing `Workspace` class           | Internal | Stable  | Factory wraps it; no changes required  | Dev     |
| `mlody/roots.mlody` at target commit | Content  | Unknown | Sentinel check and workspace load fail | Authors |

---

## 18. Open Questions & Action Items

| ID   | Question / Action                                                                                       | Owner | Target Date | Status   |
| ---- | ------------------------------------------------------------------------------------------------------- | ----- | ----------- | -------- |
| OQ-1 | Mixing committoid-qualified and cwd-relative targets in one `show` invocation — parse error or allowed? | Dev   | TBD         | Open     |
| OQ-2 | When branch and tag share the same name: prefer branch, or error?                                       | Dev   | TBD         | Open     |
| OQ-3 | Exact `git` invocation for local SHA checkout (SHA not always valid as `--branch` argument)             | Dev   | TBD         | Open     |
| OQ-4 | Should resolved SHA be printed to stdout as a comment, or only to `--verbose` log?                      | UX    | TBD         | Open     |
| OQ-5 | Future: support for remotes other than `origin`                                                         | Dev   | Future      | Deferred |
| OQ-6 | Future: cache cleanup / eviction policy                                                                 | Dev   | Future      | Deferred |
| OQ-7 | Future: mixed-version graphs (different committoid per node in one evaluation)                          | Arch  | Future      | Deferred |
| OQ-8 | Future: container image build integration — exact interface to workspace cache                          | Arch  | Future      | Deferred |

---

## 19. Revision History

| Version | Date       | Author                  | Changes       |
| ------- | ---------- | ----------------------- | ------------- |
| 1.0     | 2026-03-07 | Requirements Analyst AI | Initial draft |

---

## Appendices

### Appendix A: Glossary

| Term            | Definition                                                                               |
| --------------- | ---------------------------------------------------------------------------------------- |
| Committoid      | A user-supplied string identifying a commit: full or short SHA, branch name, or tag name |
| Workspace       | An instance of `mlody.core.workspace.Workspace` built from a specific source tree root   |
| Cache hit       | The workspace directory for a given SHA exists and passes the sentinel check             |
| Sentinel file   | `mlody/roots.mlody` — its presence in a cached workspace indicates a complete checkout   |
| Inner label     | The `@`- or `//`-prefixed target address that follows the `\|` delimiter                 |
| Lock file       | `<SHA>.lock` in the cache directory, used to serialise materialisation                   |
| Materialisation | The process of cloning a source tree into the cache directory for a given SHA            |

### Appendix B: Resolution Algorithm

```
parse_label(raw):
  if raw starts with '@' or '//':
    return (committoid=None, inner_label=raw)       # cwd path
  split on first '|' → (committoid_part, inner_label)
  if inner_label does not start with '@' or '//': error
  return (committoid_part, inner_label)

resolve_sha(committoid):
  # Always go to the remote for canonical resolution and ambiguity detection
  results = git ls-remote origin <committoid>
  if len(results) == 0: error("unknown ref: <committoid>")
  if len(distinct SHAs in results) > 1: error("ambiguous ref: <committoid>")
  full_sha = single resolved SHA (dereference annotated tags via ^{})
  return full_sha

choose_clone_strategy(full_sha, monorepo_root):
  # Local check is a clone-strategy hint only, not a resolution step
  if git cat-file -t <full_sha> in monorepo_root == 'commit':
    return LOCAL
  return REMOTE

materialise(full_sha, monorepo_root, strategy):
  if cache_hit(full_sha): return cache_dir(full_sha)   # no clone needed
  acquire lock(full_sha) or error immediately
  try:
    if strategy == LOCAL:
      git clone --local --depth 1 file:///monorepo_root → cache_dir(full_sha)
    else:
      git clone --depth 1 origin → cache_dir(full_sha); checkout full_sha
    write_meta(full_sha)
  finally:
    release lock(full_sha)
  return cache_dir(full_sha)
```

### Appendix C: Metadata Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema",
  "type": "object",
  "required": ["requested_ref", "resolved_sha", "resolved_at", "repo"],
  "properties": {
    "requested_ref": { "type": "string" },
    "resolved_sha": { "type": "string", "pattern": "^[0-9a-f]{40}$" },
    "resolved_at": { "type": "string", "format": "date-time" },
    "repo": { "type": "string" }
  }
}
```

---

**End of Requirements Document**
