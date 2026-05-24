"""Phase 3: OCI image build using a dynamically-generated BUILD.bazel."""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

from mlody.common.image_builder.errors import BazelBuildError
from mlody.common.image_builder.log import info
from mlody.common.image_builder.phases.clone import CloneResult

# Package written into the clone dir for OCI image assembly.
# Leading underscore keeps it out of normal Bazel query results.
_DYN_PKG = "_dynamic_image"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


@dataclasses.dataclass(frozen=True)
class BazelResult:
    stdout: str
    stderr: str


def _target_subdir(label: str) -> str:
    """Derive the subdirectory path for a target's outputs inside the image.

    Uses the full package path + target name to guarantee uniqueness across
    the workspace, mirroring the source tree layout.

      //mlody/cli:mlody              -> mlody/cli/mlody
      //repo/smoketest/python/simple:simple  -> repo/smoketest/python/simple/simple
      @repo//pkg:name                -> pkg/name
    """
    # Strip leading @ repo qualifier and //
    label = label.lstrip("@")
    if "//" in label:
        label = label.split("//", 1)[1]
    # Split package path and target name
    if ":" in label:
        pkg, name = label.split(":", 1)
    else:
        pkg = label.rstrip("/")
        name = pkg.split("/")[-1]
    return f"{pkg}/{name}" if pkg else name


def _safe_rule_name(subdir: str) -> str:
    """Convert a subdirectory name to a valid Starlark rule name."""
    return _SAFE_NAME_RE.sub("_", subdir)


def _build_labels(sha: str, clone_result: CloneResult) -> dict[str, str]:
    """Derive OCI image labels from the clone result."""
    dirty = bool(clone_result.applied_patch or clone_result.applied_untracked)
    labels: dict[str, str] = {
        "org.opencontainers.image.revision": sha,
        "com.polymath.mlody.dirty": "true" if dirty else "false",
    }
    if dirty:
        changed_files = len(
            {
                line
                for line in clone_result.applied_patch.splitlines()
                if line.startswith("diff ")
            }
        )
        labels["com.polymath.mlody.dirty_files_changed"] = str(changed_files)
        labels["com.polymath.mlody.dirty_untracked"] = str(
            len(clone_result.applied_untracked)
        )
    return labels


def _query_python_targets(clone_dir: Path, targets: list[str]) -> frozenset[str]:
    """Return the subset of targets that are py_binary rules.

    Uses `bazel query` inside the clone workspace to identify Python binary
    targets so they can be packaged with py_image_layer (full runfiles tree:
    interpreter, pip packages, all source files) rather than pkg_tar (which
    only captures the binary launcher without its runfiles).

    Returns an empty frozenset on query failure, causing callers to fall back
    to pkg_tar for all targets.
    """
    if not targets:
        return frozenset()
    target_set = " ".join(targets)
    result = subprocess.run(
        ["bazel", "query", f"kind('py_binary', set({target_set}))"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def _write_image_build(
    clone_dir: Path,
    targets: list[str],
    labels: dict[str, str],
    base_image: str,
    python_targets: frozenset[str],
) -> None:
    """Write a BUILD.bazel for OCI image assembly into the clone workspace.

    Python targets use py_image_layer which bundles the full runfiles tree
    (source files, pip packages, Python interpreter) so imports work at
    runtime. Non-Python targets use pkg_tar.

    All layers are combined in a single oci_image target.
    """
    pkg_dir = clone_dir / _DYN_PKG
    pkg_dir.mkdir(exist_ok=True)

    py_layer_vars: list[str] = []
    other_rules: list[str] = []
    other_tar_names: list[str] = []

    for label in targets:
        safe = _safe_rule_name(_target_subdir(label))
        if label in python_targets:
            var_name = f"_layers_{safe}"
            py_layer_vars.append(var_name)
            other_rules.append(
                f"{var_name} = py_image_layer(\n"
                f'    name = "py_layer_{safe}",\n'
                f'    binary = "{label}",\n'
                f")\n"
            )
        else:
            subdir = _target_subdir(label)
            rule_name = f"layer_{safe}"
            other_tar_names.append(f'":layer_{safe}"')
            other_rules.append(
                f"pkg_tar(\n"
                f'    name = "{rule_name}",\n'
                f'    srcs = ["{label}"],\n'
                f'    package_dir = "/{subdir}",\n'
                f")\n"
            )

    # Compose the tars= expression: py_image_layer list vars + pkg_tar label list
    if py_layer_vars and other_tar_names:
        pkg_part = "[\n        " + ",\n        ".join(other_tar_names) + ",\n    ]"
        tars_expr = " + ".join(py_layer_vars) + f" + {pkg_part}"
    elif py_layer_vars:
        tars_expr = " + ".join(py_layer_vars)
    else:
        tars_expr = "[\n        " + ",\n        ".join(other_tar_names) + ",\n    ]"

    labels_starlark = "\n".join(
        f'        "{k}": "{v}",' for k, v in labels.items()
    )
    rules_block = "\n".join(other_rules)

    load_py_image_layer = (
        'load("@aspect_rules_py//py:defs.bzl", "py_image_layer")\n'
        if py_layer_vars
        else ""
    )
    load_pkg_tar = (
        'load("@rules_pkg//pkg:tar.bzl", "pkg_tar")\n' if other_tar_names else ""
    )

    content = f"""\
# Auto-generated by mlody-image-builder — do not commit.
load("@rules_oci//oci:defs.bzl", "oci_image")
{load_pkg_tar}{load_py_image_layer}
{rules_block}
oci_image(
    name = "image",
    base = "{base_image}",
    tars = {tars_expr},
    labels = {{
{labels_starlark}
    }},
)
"""
    (pkg_dir / "BUILD.bazel").write_text(content)


def run_bazel_build(
    sha: str,
    clone_result: CloneResult,
    targets: list[str],
    base_image: str = "@debian_slim",
) -> BazelResult:
    """Build the combined OCI image inside the clone directory.

    Queries for Python binary targets and uses py_image_layer for them
    (includes full runfiles tree) and pkg_tar for all others, then invokes
    `bazel build //_dynamic_image:image`.

    Raises BazelBuildError on non-zero bazel exit.
    """
    clone_dir = clone_result.path
    labels = _build_labels(sha, clone_result)
    python_targets = _query_python_targets(clone_dir, targets)
    _write_image_build(clone_dir, targets, labels, base_image, python_targets)

    # Build all named targets in the package, not just :image.  When :image is
    # a Bazel cache hit its layer dependencies (py_image_layer tar.gz outputs)
    # may not be materialised on disk — only the cached oci_image layout is
    # restored.  Requesting :all forces Bazel to also materialise the layer
    # targets' outputs, so the symlinks in the image layout resolve correctly.
    target = f"//{_DYN_PKG}:all"
    cmd = ["bazel", "build", target]
    info("build", targets=targets, cmd=" ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BazelBuildError(
            f"bazel build {target} failed",
            targets=targets,
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )

    info("build", status="success", targets=targets)
    return BazelResult(stdout=result.stdout, stderr=result.stderr)
