# Requirements Document: Sparse Clone for Workspace Materialisation

**Version:** 1.1 **Date:** 2026-03-08 **Prepared by:** Requirements Analyst AI
**Status:** Draft

---

## 1. Executive Summary

When mlody resolves a committoid-qualified label (e.g. `main|@lexica//:bert`),
it materialises a workspace clone of the omega monorepo at the requested SHA
under `~/.cache/mlody/workspaces/`. Today the clone fetches every file in the
repository. The monorepo is under 1 GB, but `mlody/docs` contains heavy binary
images that are irrelevant to evaluation and contribute meaningfully to clone
time and disk consumption.

The goal is to replace the full-tree clone in `GitClient.clone_local` and
`GitClient.clone_remote` with a git sparse-checkout clone that transfers only a
configurable list of top-level directories (initially just `mlody`), while
explicitly excluding `mlody/docs` from that set. The configurable include-list
must be defined in one canonical place in the Python code so that future
additions to the `mlody/` subtree (or other relevant monorepo directories) can
be enabled without touching the clone logic itself.

The expected outcomes are faster materialisation time for on-demand "show"
operations and reduced disk usage in `~/.cache/mlody/workspaces/`.

---

## 2. Project Scope

### 2.1 In Scope

- Modify `GitClient.clone_remote` to perform a sparse, partial-tree clone using
  non-cone pattern mode (`--no-cone`) instead of a full clone.
- Introduce two module-level constants in `mlody/resolver/git_client.py`:
  `SPARSE_INCLUDE` (initial value `["mlody"]`) and `SPARSE_EXCLUDE` (initial
  value `["mlody/docs"]`) that drive the sparse-checkout pattern set.
- Update existing tests in `git_client_test.py` to assert the new sparse
  checkout commands are issued correctly.
- Add new tests covering the configurable include/exclude behaviour, using the
  constants' importability to override them in tests without subclassing.

### 2.2 Out of Scope

- `GitClient.clone_local` — the local clone path is unchanged. The local object
  store already has all blobs; `--filter=blob:none` and sparse-checkout are not
  applied there (OQ-003 resolved).
- Cache lifetime management (TTL, eviction, reuse across calls) — explicitly
  deferred.
- Any changes to `resolver.py`, `cache.py`, or `workspace.py`.
- CI pipeline configuration changes.
- Developer workstation setup scripts.
- Any UI or CLI flag for end-users to override the include/exclude list at
  invocation time.

### 2.3 Assumptions

- **Assumption A1 [RESOLVED]:** Git cone mode does NOT support negative/exclude
  patterns. Attempting to use `!`-prefixed entries in cone mode causes git to
  emit `warning: unrecognized negative pattern` and silently disable cone
  matching. The implementation must therefore use non-cone (pattern) mode via
  `git sparse-checkout set --no-cone` with gitignore-style patterns, including
  `!mlody/docs/` as a negation entry. See OQ-002 resolution in Section 18 and
  the updated command sequences in Appendix D for details. The performance
  trade-off (non-cone mode is O(N×M) against the index vs. cone mode's hash
  lookup) is acceptable given the small number of patterns involved.
- **Assumption A2 [RESOLVED — OQ-003]:** `clone_local` does NOT gain
  `--filter=blob:none`. The local clone path is out of scope for this change.
  The local object store already contains all blobs, so the blobless filter
  would add overhead without benefit. Only `clone_remote` is modified.
- **Assumption A3:** The cache sentinel `mlody/roots.mlody` will be present
  inside the sparse checkout because `mlody` is an included directory and
  `mlody/roots.mlody` is not under `mlody/docs`.
- **Assumption A4:** No other code path directly calls `git clone` on the
  monorepo; `GitClient.clone_local` and `GitClient.clone_remote` are the sole
  entry points.

### 2.4 Constraints

- All subprocess calls must continue to use list arguments (no `shell=True`) to
  preserve the existing shell-injection protection.
- Authentication must not change: `clone_local` uses `file://` transport;
  `clone_remote` uses `origin` (whatever remote URL is configured in the
  caller's working copy, typically SSH with a host alias).
- The include/exclude list is a code-level constant, not a runtime config file
  or CLI flag. It must live in a single, obvious location (see Section 6).
- Python 3.13.2, strict basedpyright type checking, ruff formatting — all
  existing code-quality constraints apply.

---

## 3. Stakeholders

| Role                | Name/Group                     | Responsibilities                                     | Contact |
| ------------------- | ------------------------------ | ---------------------------------------------------- | ------- |
| Primary User        | mlody end-users                | Invoke `mlody show` with committoid-qualified labels | n/a     |
| Code Owner          | Polymath Solutions engineering | Review and merge the change                          | n/a     |
| Requirements Author | Requirements Analyst AI        | This document                                        | n/a     |

---

## 4. Business Requirements

### 4.1 Business Objectives

- **BR-001:** Reduce the time to materialise a workspace for an on-demand "show"
  operation by eliminating the transfer of binary assets (images in
  `mlody/docs`) that are not needed for value resolution.
- **BR-002:** Reduce disk usage in `~/.cache/mlody/workspaces/` by omitting
  irrelevant subtrees from materialised clones.
- **BR-003:** Keep the include/exclude list maintainable so that future mlody
  subtrees can be added or removed without touching clone mechanics.

### 4.2 Success Metrics

- **KPI-001:** Clone time — Target: measurably lower than current full clone on
  a cold cache; exact baseline TBD after measurement. Measurement: wall-clock
  time of `materialise()` on a cache miss.
- **KPI-002:** Clone disk footprint — Target: no `mlody/docs` directory present
  in any entry under `~/.cache/mlody/workspaces/`. Measurement: `ls` of a
  materialised cache entry after the change.
- **KPI-003:** Correctness — Target: `mlody/roots.mlody` sentinel is present in
  every successfully materialised cache entry, and `check_cache` returns `"hit"`
  after materialisation. Measurement: existing cache test suite passes.

---

## 5. User Requirements

### 5.1 User Personas

**Persona 1: mlody end-user**

- Runs `mlody show <committoid>|<label>` to inspect a value at a historical
  commit.
- Has no knowledge of the underlying git mechanics.
- Expects the command to complete faster than a full monorepo clone.
- Does not interact with the sparse-checkout configuration.

**Persona 2: mlody contributor**

- Adds a new top-level directory to `mlody/` in the future.
- Needs to add that directory to the include-list in one place without
  understanding the git sparse-checkout command sequence.

### 5.2 User Stories

**Epic 1: Faster on-demand workspace materialisation**

- **US-001:** As a mlody end-user, I want `mlody show` at a remote commit to
  complete faster and use less disk space, so that historical lookups feel
  responsive.
  - Acceptance Criteria: Given a cache miss for a committoid, when
    `materialise()` runs, then the resulting directory under
    `~/.cache/mlody/workspaces/` contains `mlody/` but does not contain
    `mlody/docs/`.
  - Priority: High

**Epic 2: Maintainable include/exclude configuration**

- **US-002:** As a mlody contributor, I want the list of sparse-checkout
  directories to be defined in a single, clearly labelled constant, so that I
  can add a new directory without reading the git command internals.
  - Acceptance Criteria: Given a new directory name added to the include
    constant, when `clone_local` or `clone_remote` is called, then that
    directory is included in the sparse checkout without any other code changes.
  - Priority: High

---

## 6. Functional Requirements

### 6.1 Sparse-Checkout Configuration Constant

**FR-001: Single-source include/exclude configuration**

- Description: Introduce two module-level constants in
  `mlody/resolver/git_client.py` that declare the full non-cone sparse-checkout
  pattern set. These constants are the sole place where included and excluded
  paths are listed. They are NOT injected into `GitClient.__init__`; instead,
  they are importable at module level so tests can monkeypatch or shadow them
  without subclassing (OQ-005 resolved).
- Constants:
  - `SPARSE_INCLUDE: list[str]` — top-level directory names to include; initial
    value `["mlody"]`.
  - `SPARSE_EXCLUDE: list[str]` — sub-paths to exclude within included
    directories; initial value `["mlody/docs"]`.
- Business Rules:
  - `SPARSE_INCLUDE` and `SPARSE_EXCLUDE` are module-level; they must not be
    computed at call time from external state (no environment variables, no file
    reads).
  - The implementation translates these two lists into the gitignore-style
    pattern sequence required by `git sparse-checkout set --no-cone` (see
    Appendix D for the derived pattern set).
- Priority: Must Have
- Dependencies: None

### 6.2 Local Clone — No Change

**FR-002: `clone_local` is unchanged**

- Description: `GitClient.clone_local` is out of scope for this change (OQ-003
  resolved). The local object store already contains all blobs; applying
  `--filter=blob:none` or sparse-checkout there would add overhead without
  benefit. The existing command sequence (`git clone --local --no-checkout` +
  fetch + checkout) is preserved verbatim.
- Priority: Must Have (as a constraint — do not touch this path)
- Dependencies: None

### 6.3 Sparse Remote Clone

**FR-003: `clone_remote` issues non-cone sparse-checkout commands**

- Description: Replace the current `git clone --filter=blob:none --no-checkout`
  sequence in `GitClient.clone_remote` with a sequence that configures non-cone
  sparse-checkout using the patterns derived from FR-001. Cone mode is NOT used
  because it does not support negative/exclude patterns (Assumption A1 resolved;
  see OQ-002 in Section 18).
- Required git command sequence (logical order):
  1. `git clone --filter=blob:none --no-checkout --sparse <remote_url> <dest>`
  2. `git -C <dest> sparse-checkout set --no-cone <patterns...>` where
     `<patterns...>` is the gitignore-style set derived from `SPARSE_INCLUDE`
     and `SPARSE_EXCLUDE` (see Appendix D).
  3. `git -C <dest> fetch --depth 1 origin <sha>` (existing fetch, unchanged).
  4. `git -C <dest> checkout <sha>` (existing checkout, unchanged).
- Note: `--filter=blob:none` is already present in `clone_remote`; it must be
  retained. Adding `--sparse` activates the sparse-checkout index in the new
  clone.
- Inputs: `dest: Path`, `sha: str` (unchanged); patterns sourced from FR-001
  constants.
- Outputs: `dest` populated with only the included paths at `sha`, with
  `mlody/docs/` absent.
- Business Rules:
  - `shell=True` must not be used.
  - On any subprocess failure, `GitNetworkError` must be raised (existing
    behaviour).
- Priority: Must Have
- Dependencies: FR-001

### 6.4 Authentication Unchanged

**FR-004: Clone authentication is not modified**

- Description: The remote URL for `clone_remote` comes from `origin` as
  configured in the caller's working copy. No changes to remote URL resolution,
  SSH config, or credential helpers are introduced.
- Priority: Must Have
- Dependencies: None

### 6.5 Sentinel Reachability

**FR-005: Cache sentinel remains reachable post-sparse-checkout**

- Description: `mlody/roots.mlody` (the sentinel used by `check_cache` to
  determine a "hit") must be present in the sparse-checked-out clone. Because
  `mlody` is in `SPARSE_INCLUDE` and `mlody/roots.mlody` is not under
  `mlody/docs`, this should hold by construction.
- Acceptance Criteria: After `clone_local` or `clone_remote` completes
  successfully, `<dest>/mlody/roots.mlody` exists.
- Priority: Must Have
- Dependencies: FR-001, FR-002, FR-003

---

## 7. Non-Functional Requirements

### 7.1 Performance Requirements

- **NFR-P-001:** The sparse clone must transfer fewer bytes than the current
  full clone on a cold cache. Exact targets are not specified; any measurable
  improvement is acceptable for the initial release.
- **NFR-P-002:** The sparse clone must not introduce additional network round
  trips beyond those already present (clone + fetch + checkout).

### 7.2 Scalability Requirements

- **NFR-S-001:** Adding new entries to `SPARSE_INCLUDE` or `SPARSE_EXCLUDE` must
  not require changes to the git command construction logic.

### 7.3 Availability and Reliability

- No change from existing behaviour. Clone failures already raise
  `GitNetworkError`; that contract is preserved.

### 7.4 Security Requirements

- **NFR-SEC-001:** All git subprocess calls must continue to use list arguments
  (no `shell=True`, no string interpolation into command strings). This is an
  existing requirement; the new commands must comply.
- **NFR-SEC-002:** Cache directory permissions (`mode=0o700`) set by
  `ensure_cache_root` are not changed.

### 7.5 Usability Requirements

- The change is invisible to end-users. No new CLI flags, prompts, or output is
  introduced.

### 7.6 Maintainability Requirements

- **NFR-M-001:** The include/exclude patterns must be co-located with
  `GitClient` in `git_client.py`, not scattered across caller modules.
- **NFR-M-002:** The constant must have a docstring or inline comment explaining
  that adding an entry here is sufficient to extend the sparse checkout.

### 7.7 Compatibility Requirements

- **NFR-C-001 [RESOLVED — OQ-001]:** Target git version is 2.47.3. This version
  fully supports `git clone --sparse`, `git sparse-checkout set --no-cone`, and
  all flags used in the target command sequences. The team will upgrade git if a
  newer version is required by future changes; no artificial feature cap is
  imposed on the design.

---

## 8. Data Requirements

### 8.1 Data Entities

No new data entities. The existing cache entry structure
(`~/.cache/mlody/workspaces/<sha>/`) and metadata file
(`~/.cache/mlody/workspaces/<sha>-meta.json`) are unchanged.

### 8.2 Data Quality Requirements

The sentinel file `mlody/roots.mlody` must be present in every successfully
materialised sparse clone (see FR-005).

### 8.3 Data Retention and Archival

Out of scope (see Section 2.2).

### 8.4 Data Privacy and Compliance

No change. Cache entries are written with `mode=0o700` (user-only) as today.

---

## 9. Integration Requirements

### 9.1 External Systems

| System                         | Purpose                           | Change                                                                      |
| ------------------------------ | --------------------------------- | --------------------------------------------------------------------------- |
| Git CLI                        | Sparse-checkout clone             | New flags added to existing `git clone` and `git sparse-checkout set` calls |
| Origin remote (SSH)            | Source of monorepo at a given SHA | No change — URL, auth, and transport are unchanged                          |
| Local object store (`file://`) | Source for local clones           | No change — `file://` URL construction is preserved                         |

### 9.2 API Requirements

`GitClient.clone_local(dest, sha)` and `GitClient.clone_remote(dest, sha)` —
public signatures are unchanged. The sparse-checkout patterns are drawn from the
module-level constant (FR-001), not from method parameters, so call sites in
`resolver.py` require no modification.

---

## 10. User Interface Requirements

Not applicable. This change has no user-visible interface.

---

## 11. Reporting and Analytics Requirements

Not applicable.

---

## 12. Security and Compliance Requirements

### 12.1 Authentication and Authorization

No change. SSH key-based auth via the `origin` remote alias is reused as-is.

### 12.2 Data Security

Cache directory permissions are unchanged (`0o700`).

### 12.3 Compliance

No regulatory compliance requirements identified.

### 12.4 Permission Matrix

Not applicable for this change.

---

## 13. Infrastructure and Deployment Requirements

### 13.1 Hosting and Environment

Materialisation runs on developer machines (wherever `mlody show` is invoked).
No server-side infrastructure is involved.

### 13.2 Deployment

The change ships as a normal Python source update within the Bazel monorepo. No
migration steps are required for existing cache entries — they were produced by
full clones and will continue to be valid hits (the sentinel is present).

### 13.3 Disaster Recovery

Not applicable.

---

## 14. Testing and Quality Assurance Requirements

### 14.1 Testing Scope

- **UT-001:** `TestCloneLocal` — assert that `clone_local` command sequence is
  UNCHANGED (no `--sparse`, no `--filter=blob:none`, no `sparse-checkout set`
  call). This guards against accidental regression.
- **UT-002:** `TestCloneRemote` — assert that the `git clone` call includes
  `--sparse` and `--filter=blob:none`, and that a subsequent
  `git sparse-checkout set --no-cone <patterns>` call is issued with the correct
  gitignore-style pattern set derived from `SPARSE_INCLUDE` and
  `SPARSE_EXCLUDE`.
- **UT-003:** Exclusion assertion — assert that `mlody/docs` in `SPARSE_EXCLUDE`
  produces a `!mlody/docs/` negation entry in the sparse-checkout pattern list
  passed to git.
- **UT-004:** Constant extension — by monkeypatching `SPARSE_INCLUDE` in the
  test (no subclassing), assert that adding a second directory causes it to
  appear in the `git sparse-checkout set --no-cone` call without any other code
  changes.
- All existing tests in `git_client_test.py` must continue to pass.

### 14.2 Acceptance Criteria

A materialised cache entry produced by the new code:

1. Does not contain `mlody/docs/`.
2. Does contain `mlody/roots.mlody`.
3. Passes `check_cache(cache_root, sha) == "hit"`.

---

## 15. Training and Documentation Requirements

### 15.1 User Documentation

None required. The change is transparent to users.

### 15.2 Technical Documentation

- The module-level constant in `git_client.py` must have a docstring or block
  comment explaining its purpose and how to extend it.
- Inline comments on the new git command sequences explaining why each flag is
  used (consistent with the existing comment style in the file).

### 15.3 Training

None required.

---

## 16. Risks and Mitigation Strategies

| Risk ID | Description                                                                                               | Impact                                                  | Probability | Mitigation                                                                                                 | Owner          |
| ------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------- | -------------- |
| R-001   | RESOLVED — Cone mode exclusions unsupported; non-cone mode mandated (OQ-002)                              | n/a                                                     | n/a         | Design uses `--no-cone` + gitignore patterns from the start; no fallback branching needed                  | Closed         |
| R-002   | RESOLVED — git version confirmed as 2.47.3; all required flags are available (OQ-001)                     | n/a                                                     | n/a         | No version guard needed                                                                                    | Closed         |
| R-003   | RESOLVED — `clone_local` is out of scope; `--filter=blob:none` is not applied locally (OQ-003)            | n/a                                                     | n/a         | No action needed                                                                                           | Closed         |
| R-004   | Existing corrupt cache entries (directory present, sentinel absent) produced by old full clones interfere | Low — `check_cache` already handles "corrupt" state     | Low         | No mitigation needed; `CorruptCacheError` is already raised and callers are instructed to delete and retry | Existing code  |
| R-005   | Non-cone mode O(N×M) index scan is slower than cone mode hash lookup                                      | Low — pattern count is tiny (2 includes, 1 exclude now) | Low         | Acceptable for current scale; revisit only if the pattern list grows large enough to affect performance    | Implementation |

---

## 17. Dependencies

| Dependency                                  | Type            | Status                                                                            | Impact if Delayed | Owner          |
| ------------------------------------------- | --------------- | --------------------------------------------------------------------------------- | ----------------- | -------------- |
| Git 2.47.3 on target machines               | Runtime         | RESOLVED — confirmed (OQ-001)                                                     | n/a               | Infrastructure |
| Cone mode exclusion support (Assumption A1) | Design decision | RESOLVED — cone mode does NOT support exclusions; non-cone mode mandated (OQ-002) | n/a               | Closed         |

---

## 18. Open Questions and Action Items

| ID     | Question/Action                                                                                                       | Owner          | Target Date | Status                                                                                                                                                                                                                                                                                                                               |
| ------ | --------------------------------------------------------------------------------------------------------------------- | -------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| OQ-001 | What git version is available on the machines where `mlody show` is invoked?                                          | Stakeholder    | 2026-03-08  | RESOLVED — git 2.47.3. The team will upgrade git if a newer version is needed; no artificial feature cap is imposed. NFR-C-001 and Appendix D updated accordingly.                                                                                                                                                                   |
| OQ-002 | Does git cone mode support negative/exclude patterns (e.g. `!mlody/docs`)?                                            | Implementation | 2026-03-08  | RESOLVED — No. Cone mode does NOT support negative patterns. Attempting them causes git to emit `warning: unrecognized negative pattern` and disable cone matching. The implementation MUST use `git sparse-checkout set --no-cone` with gitignore-style patterns. Assumption A1 updated; FR-003 and Appendix D updated accordingly. |
| OQ-003 | Should `clone_local` also gain `--filter=blob:none` and sparse-checkout?                                              | Implementation | 2026-03-08  | RESOLVED — No. `clone_local` is out of scope. The local object store already has all blobs; applying the filter adds overhead without benefit. FR-002 updated to reflect no change on the local path. Section 2.2 updated accordingly.                                                                                               |
| OQ-004 | Are there other mlody subdirectories to exclude beyond `mlody/docs`?                                                  | Stakeholder    | 2026-03-08  | RESOLVED — No. Only `mlody/docs` is excluded at this time. `SPARSE_INCLUDE` starts as `["mlody"]`; `SPARSE_EXCLUDE` starts as `["mlody/docs"]`. Future subdirectories can be added to the constants without touching clone logic.                                                                                                    |
| OQ-005 | Should `SPARSE_INCLUDE` / `SPARSE_EXCLUDE` be constructor-injected into `GitClient` or remain module-level constants? | Implementation | 2026-03-08  | RESOLVED — Module-level constants in `git_client.py`. Testability is achieved via direct import and monkeypatching, not subclassing. FR-001 and UT-004 updated accordingly.                                                                                                                                                          |

---

## 19. Revision History

| Version | Date       | Author                  | Changes                                                                                                                                                                         |
| ------- | ---------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-03-08 | Requirements Analyst AI | Initial draft                                                                                                                                                                   |
| 1.1     | 2026-03-08 | Requirements Analyst AI | Resolved OQ-001 through OQ-005; mandated non-cone mode; scoped clone_local out; confirmed git 2.47.3; locked down module-level constants pattern; updated all affected sections |

---

## Appendices

### Appendix A: Glossary

| Term                 | Definition                                                                                                                                |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| committoid           | A branch name, tag name, short SHA, or full SHA used to identify a specific commit in the monorepo                                        |
| cone mode            | A git sparse-checkout mode where entire directory subtrees (rather than individual file patterns) are specified; faster index performance |
| materialise          | The process of cloning the monorepo at a specific SHA into the local cache and verifying the sentinel file                                |
| sentinel             | The file `mlody/roots.mlody` whose presence marks a cache entry as complete                                                               |
| sparse-checkout      | A git feature that checks out only a subset of the working tree                                                                           |
| SPARSE_EXCLUDE       | Proposed constant listing sub-paths to omit from the sparse checkout                                                                      |
| SPARSE_INCLUDE       | Proposed constant listing top-level directories to include in the sparse checkout                                                         |
| worktree / workspace | A materialised clone at a given SHA under `~/.cache/mlody/workspaces/`                                                                    |

### Appendix B: Relevant Files

| File                                | Role                                                               |
| ----------------------------------- | ------------------------------------------------------------------ |
| `mlody/resolver/git_client.py`      | Primary change target — contains `clone_local` and `clone_remote`  |
| `mlody/resolver/git_client_test.py` | Test file — must be updated to assert new command sequences        |
| `mlody/resolver/resolver.py`        | Calls `materialise()` which calls `GitClient`; no changes expected |
| `mlody/resolver/cache.py`           | Cache sentinel logic; no changes expected                          |
| `mlody/resolver/errors.py`          | Exception hierarchy; no changes expected                           |

### Appendix C: Existing Clone Command Sequences (Baseline)

**`clone_local` today:**

```
git clone --local --no-checkout file:///<root> <dest>
git -C <dest> fetch --depth 1 origin <sha>   # may be skipped if sha is present
git -C <dest> checkout <sha>
```

**`clone_remote` today:**

```
git clone --filter=blob:none --no-checkout origin <dest>
git -C <dest> fetch --depth 1 origin <sha>
git -C <dest> checkout <sha>
```

### Appendix D: Resolved Command Sequences (Target)

All open questions are resolved. The sequences below are definitive.

**`clone_local` — UNCHANGED (OQ-003 resolved: local path is out of scope)**

```
git clone --local --no-checkout file:///<root> <dest>
git -C <dest> fetch --depth 1 origin <sha>   # may be skipped if sha is present
git -C <dest> checkout <sha>
```

**`clone_remote` — UPDATED to non-cone sparse-checkout (OQ-001, OQ-002
resolved)**

```
git clone --filter=blob:none --no-checkout --sparse <remote_url> <dest>
git -C <dest> sparse-checkout set --no-cone \
    "mlody/"       \   # include everything under mlody/
    "!mlody/docs/" \   # exclude mlody/docs/ (negation must follow its parent)
git -C <dest> fetch --depth 1 origin <sha>
git -C <dest> checkout <sha>
```

**Non-cone pattern derivation rule** (implemented in code, not hardcoded):

The implementation derives the `--no-cone` pattern list from `SPARSE_INCLUDE`
and `SPARSE_EXCLUDE` at call time using the following logic:

1. For each entry `d` in `SPARSE_INCLUDE`, emit `d/` (include the directory).
2. For each entry `e` in `SPARSE_EXCLUDE`, emit `!e/` (negate the sub-path).
   Negation entries must be ordered after the include entry for their parent
   directory so that git applies them correctly (order matters in non-cone
   mode).

Initial derived pattern set:

```
mlody/
!mlody/docs/
```

**Performance note (non-cone vs. cone mode):** Non-cone mode uses O(N×M) index
scanning (N patterns, M paths). With only 2 patterns and a monorepo of under 1
GB, this is negligible. Cone mode was not usable because it does not support
negative/exclude patterns (OQ-002 resolved).

---

**End of Requirements Document**
