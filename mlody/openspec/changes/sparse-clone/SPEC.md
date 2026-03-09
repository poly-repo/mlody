# Spec: Sparse Clone for Workspace Materialisation

**Version:** 1.0 **Date:** 2026-03-08 **Status:** Ready for Implementation

---

## 1. Executive Summary

When mlody resolves a committoid-qualified label (e.g. `main|@lexica//:bert`),
`materialise()` in `resolver.py` calls either `GitClient.clone_local` or
`GitClient.clone_remote` to produce a working-tree snapshot at the requested SHA
under `~/.cache/mlody/workspaces/<sha>/`. Today `clone_remote` fetches every
file in the monorepo. The `mlody/docs` subtree contains heavy binary images that
are never needed for value resolution, contributing meaninglessly to clone time
and cache disk usage.

This change modifies **only `mlody/resolver/git_client.py`** (and its companion
test file `git_client_test.py`). It:

1. Adds two module-level constants, `SPARSE_INCLUDE` and `SPARSE_EXCLUDE`, that
   declare the gitignore-style pattern set for non-cone sparse-checkout.
2. Modifies `GitClient.clone_remote` to perform a sparse, blobless clone using
   `--sparse` + `git sparse-checkout set --no-cone` driven by those constants.
3. Leaves `GitClient.clone_local` completely unchanged.

No other files require modification. `resolver.py` call sites, `cache.py`, and
`workspace.py` are out of scope.

---

## 2. Current State vs. Target State

### 2.1 Current `clone_remote` (lines 86-97 of `git_client.py`)

```python
def clone_remote(self, dest: Path, sha: str) -> None:
    """Clone from origin with minimal blob transfer.

    Uses --filter=blob:none to defer blob downloads until objects are
    accessed, keeping the initial clone fast. Raises GitNetworkError on
    any subprocess failure.
    """
    self._run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "origin", str(dest)]
    )
    self._run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
    self._run(["git", "-C", str(dest), "checkout", sha])
```

The git command sequence this produces:

```
git clone --filter=blob:none --no-checkout origin <dest>
git -C <dest> fetch --depth 1 origin <sha>
git -C <dest> checkout <sha>
```

This is a full-tree clone (every file checked out at `<sha>`).

### 2.2 Target `clone_remote`

The target sequence is (given initial constants `SPARSE_INCLUDE = ["mlody"]` and
`SPARSE_EXCLUDE = ["mlody/docs"]`):

```
git clone --filter=blob:none --no-checkout --sparse origin <dest>
git -C <dest> sparse-checkout set --no-cone mlody/ !mlody/docs/
git -C <dest> fetch --depth 1 origin <sha>
git -C <dest> checkout <sha>
```

Only the `mlody/` subtree is checked out; `mlody/docs/` is excluded. Steps 3 and
4 (fetch and checkout) are identical to today.

### 2.3 `clone_local` — Unchanged

`clone_local` is explicitly out of scope. The local object store already
contains all blobs; applying `--filter=blob:none` or sparse-checkout there would
add overhead without benefit. The existing three-step sequence
(`git clone --local --no-checkout` + fetch + checkout) is preserved verbatim.

---

## 3. Constants: `SPARSE_INCLUDE` and `SPARSE_EXCLUDE`

### 3.1 Location

Both constants are module-level in `mlody/resolver/git_client.py`, placed
directly above the `GitClient` class definition. They must not be inside
`__init__`, a dataclass, or any other enclosing scope — module-level placement
is what allows tests to monkeypatch them without subclassing.

### 3.2 Definitions

```python
# Top-level directories to include in the sparse checkout. Add an entry here
# to extend the sparse checkout to a new monorepo subtree — no changes to
# clone logic are needed.
SPARSE_INCLUDE: list[str] = ["mlody"]

# Sub-paths within included directories to exclude. Add an entry here to drop
# a subtree that is irrelevant to value resolution (e.g. large binary assets).
SPARSE_EXCLUDE: list[str] = ["mlody/docs"]
```

Both are annotated with `list[str]` to satisfy basedpyright strict mode.

### 3.3 Pattern Derivation Rule

The implementation converts the two constants into the gitignore-style pattern
list required by `git sparse-checkout set --no-cone` at call time using this
rule:

1. For each `d` in `SPARSE_INCLUDE`, emit `f"{d}/"` — include the directory.
2. For each `e` in `SPARSE_EXCLUDE`, emit `f"!{e}/"` — negate the sub-path.

Negation entries must follow their parent's include entry in the list (git
applies patterns in order in non-cone mode; an `!exclude/` before its parent's
`include/` has no effect).

With the initial constants the derived list is:

```python
["mlody/", "!mlody/docs/"]
```

These are passed as variadic positional arguments to the `sparse-checkout set`
subcommand, not as a single joined string.

### 3.4 Why Non-Cone Mode

Cone mode (`git sparse-checkout set`) does **not** support negative/exclude
patterns. Attempting `!`-prefixed entries in cone mode causes git to emit
`warning: unrecognized negative pattern` and silently disables cone matching
entirely. Non-cone mode (`git sparse-checkout set --no-cone`) uses
gitignore-style patterns and fully supports negation. The performance trade-off
(O(N x M) index scan vs. cone mode's hash lookup) is negligible given the small
pattern count. Target git version is 2.47.3, which fully supports all flags
used.

---

## 4. Modified `clone_remote` — Full Specification

### 4.1 Signature

The public signature is unchanged:

```python
def clone_remote(self, dest: Path, sha: str) -> None:
```

`resolver.py` calls `git_client.clone_remote(dest=dest, sha=full_sha)` (line 142
of `resolver.py`) and requires no modification.

### 4.2 Step-by-Step Logic

```
Step 1  Build the sparse-checkout pattern list from SPARSE_INCLUDE and SPARSE_EXCLUDE.

Step 2  Run: git clone --filter=blob:none --no-checkout --sparse origin <dest>
        - --filter=blob:none: defer blob download (existing behaviour, retained)
        - --no-checkout: do not check out the working tree yet (existing behaviour, retained)
        - --sparse: activate the sparse-checkout index in the new clone

Step 3  Run: git -C <dest> sparse-checkout set --no-cone <pattern> [<pattern> ...]
        - --no-cone: non-cone (gitignore-pattern) mode
        - patterns: the list built in Step 1, e.g. ["mlody/", "!mlody/docs/"]
        - Each pattern is a separate list element (no shell quoting needed)

Step 4  Run: git -C <dest> fetch --depth 1 origin <sha>
        (unchanged from current implementation)

Step 5  Run: git -C <dest> checkout <sha>
        (unchanged from current implementation)
```

All five steps call `self._run(...)` with a list argument. `shell=False` is the
default in `subprocess.run` and must not be overridden. `GitNetworkError` is
raised automatically by `_run` on any non-zero exit.

### 4.3 Docstring Update

The method docstring must be updated to mention the sparse-checkout behaviour.
Inline comments should explain each new flag (consistent with the existing
comment style in the class).

### 4.4 Illustrative Implementation

```python
def clone_remote(self, dest: Path, sha: str) -> None:
    """Clone from origin with sparse, blobless transfer.

    Uses --filter=blob:none to defer blob downloads and --sparse to limit
    the working tree to the paths declared in SPARSE_INCLUDE, minus the
    sub-paths in SPARSE_EXCLUDE. Raises GitNetworkError on any subprocess
    failure.
    """
    # Build gitignore-style pattern set for non-cone sparse-checkout.
    # Order matters: negations must follow their parent include entry.
    patterns: list[str] = [f"{d}/" for d in SPARSE_INCLUDE]
    patterns += [f"!{e}/" for e in SPARSE_EXCLUDE]

    # --sparse activates the sparse-checkout index in the fresh clone.
    self._run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "--sparse",
         "origin", str(dest)]
    )
    # Configure non-cone sparse-checkout patterns before fetching any objects.
    self._run(
        ["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", *patterns]
    )
    self._run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
    self._run(["git", "-C", str(dest), "checkout", sha])
```

This is illustrative. The implementer may adjust formatting, variable names, or
comment wording; the command sequences and argument ordering are normative.

---

## 5. Test Strategy

All tests live in `mlody/resolver/git_client_test.py`. The Bazel target is
`//mlody/resolver:git_client_test`. Run with:

```sh
bazel test //mlody/resolver:git_client_test --test_output=errors
```

### 5.1 Existing Tests That Must Continue to Pass

All existing test classes (`TestLsRemote`, `TestCatFileType`, `TestCloneLocal`,
`TestCloneRemote`, `TestRemoteUrl`) must pass without modification to their
assertions. The existing `TestCloneRemote` tests assert:

- `"clone"` in clone command
- `"--filter=blob:none"` in clone command
- `"--no-checkout"` in clone command
- `"origin"` in clone command
- `str(dest)` in clone command

These assertions remain valid after the change (those flags are still present).
The existing tests do **not** assert the absence of `--sparse` or the absence of
a `sparse-checkout set` call, so no existing assertions break.

### 5.2 New Tests to Add

Add the following tests, grouped into the existing `TestCloneRemote` class or a
new companion class `TestCloneRemoteSparse`. Each test uses
`patch("subprocess.run", return_value=_ok(""))` as in the rest of the file.

#### UT-002 extension: `--sparse` flag and `sparse-checkout set` call present

```
test_clone_cmd_includes_sparse_flag
  - patch subprocess.run
  - call clone_remote(dest, sha)
  - assert "--sparse" is in the first call's command list
  - assert calls[1].args[0] contains "sparse-checkout", "set", "--no-cone"
```

#### UT-003: Exclusion pattern appears as negation

```
test_sparse_checkout_patterns_include_negation_for_exclude
  - patch subprocess.run
  - call clone_remote(dest, sha)
  - extract the sparse-checkout set command (calls[1].args[0])
  - assert "!mlody/docs/" is in that command
  - assert "mlody/" is in that command (positive include)
```

#### UT-004: Monkeypatching `SPARSE_INCLUDE` adds directory to sparse-checkout

```
test_additional_include_appears_in_sparse_checkout
  - import mlody.resolver.git_client as git_client_module
  - monkeypatch git_client_module.SPARSE_INCLUDE to ["mlody", "common"]
  - patch subprocess.run
  - call clone_remote(dest, sha)
  - extract sparse-checkout set command
  - assert "common/" is in the command
  - assert "mlody/" is in the command
```

The monkeypatch approach works because `clone_remote` reads `SPARSE_INCLUDE` and
`SPARSE_EXCLUDE` by name from module scope at call time (not captured at import
time or in `__init__`). The test must reference the constant through the module
object (`git_client_module.SPARSE_INCLUDE`), not through a local alias, so that
the patch takes effect.

#### UT-001 guard: `clone_local` has no sparse flags

```
test_clone_local_does_not_use_sparse
  - patch subprocess.run
  - call clone_local(dest, sha)
  - assert "--sparse" is NOT in any command across all calls
  - assert "sparse-checkout" is NOT in any command across all calls
```

This is a regression guard. Add it to `TestCloneLocal`.

### 5.3 Test Helper Pattern

The existing helpers `_ok` and `_fail` are sufficient. No new helpers are
needed. The monkeypatch for UT-004 uses pytest's built-in `monkeypatch` fixture:

```python
def test_additional_include_appears_in_sparse_checkout(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlody.resolver.git_client as git_client_module

    monkeypatch.setattr(git_client_module, "SPARSE_INCLUDE", ["mlody", "common"])
    dest = tmp_path / "dest"
    client = GitClient(tmp_path)
    with patch("subprocess.run", return_value=_ok("")) as mock_run:
        client.clone_remote(dest, "deadbeef")
    sparse_cmd = mock_run.call_args_list[1].args[0]
    assert "common/" in sparse_cmd
    assert "mlody/" in sparse_cmd
```

---

## 6. Build and Dependency Notes

No new dependencies are introduced. `git sparse-checkout set --no-cone` is a
built-in git feature (available since git 2.25, confirmed present at 2.47.3). No
changes to `BUILD.bazel`, `pyproject.toml`, or any lock file are required.

The only files that change are:

| File                                | Change                                                        |
| ----------------------------------- | ------------------------------------------------------------- |
| `mlody/resolver/git_client.py`      | Add `SPARSE_INCLUDE`, `SPARSE_EXCLUDE`; modify `clone_remote` |
| `mlody/resolver/git_client_test.py` | Add four new test methods; existing tests unchanged           |

---

## 7. Non-Functional Requirements Checklist

| Requirement                             | How it is met                                                                                        |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| No `shell=True`                         | All `_run` calls use list arguments; `subprocess.run` defaults to `shell=False`                      |
| basedpyright strict                     | `list[str]` annotations on both constants; `patterns` local is typed                                 |
| ruff formatting                         | Standard ruff-compatible formatting; no line exceeds 88 chars                                        |
| Single source of truth for patterns     | Constants live only in `git_client.py`; no duplication in callers                                    |
| Extendable without touching clone logic | Adding to `SPARSE_INCLUDE` or `SPARSE_EXCLUDE` is sufficient                                         |
| Sentinel reachable                      | `mlody/roots.mlody` is under `mlody/` and not under `mlody/docs/`, so it is included by construction |
| Authentication unchanged                | `clone_remote` still clones from `origin`; no URL or auth changes                                    |

---

## 8. Risks and Notes for the Implementer

- **Pattern ordering matters.** Git applies non-cone patterns in order. The
  exclude entries (`!mlody/docs/`) must appear after their parent's include
  entry (`mlody/`). The derivation rule in Section 3.3 (includes first, then
  excludes) guarantees this for any constant values.

- **`--sparse` on the clone step.** The `--sparse` flag initialises the
  sparse-checkout extension in the repository config of the new clone. Without
  it, the subsequent `sparse-checkout set` call would still work but the clone
  step itself would check out the full tree. The flag must be on the `git clone`
  invocation, not on `sparse-checkout set`.

- **Existing `TestCloneRemote` assertions.** The test
  `test_runs_clone_fetch_checkout` asserts `len(calls) >= 2` implicitly by
  indexing `calls[0]`. After the change there will be four calls (clone,
  sparse-checkout set, fetch, checkout). The existing test only inspects
  `calls[0]`, so it remains valid. Verify this before adding new assertions to
  avoid false failures.

- **`monkeypatch` vs. `patch`.** UT-004 uses pytest's `monkeypatch` fixture
  (preferred over `unittest.mock.patch` for module-attribute patching in this
  codebase) to override `SPARSE_INCLUDE` at the module level. The
  `subprocess.run` patch still uses `patch(...)` as a context manager, which is
  consistent with the rest of the test file.

- **No change to `clone_local`.** Do not add `--sparse`, `--filter=blob:none`,
  or any `sparse-checkout set` call to `clone_local`. The regression test
  (UT-001 guard) will catch any accidental modification.
