"""Tests for phases/push.py — crane invocation and layout materialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import mlody
import pytest

from mlody.common.image_builder.errors import PushError
from mlody.common.image_builder.phases.push import push_image

assert mlody.__name__ == "mlody"


class _Auth:
    def env_vars(self) -> dict[str, str]:
        return {}


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


def _write_minimal_layout(image_layout: Path, digest: str) -> None:
    blob_dir = image_layout / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (image_layout / "index.json").write_text("{}")
    (image_layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (blob_dir / digest).write_text("blob-bytes")


def test_push_image_repairs_broken_blob_symlink_from_output_base(tmp_path: Path) -> None:
    digest = "d" * 64
    output_base = tmp_path / "output-base"
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(parents=True)

    exec_bin = output_base / "execroot" / "_main" / "bazel-out" / "k8-fastbuild" / "bin"
    image_layout = exec_bin / "_dynamic_image" / "image"
    blob_dir = image_layout / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (image_layout / "index.json").write_text("{}")
    (image_layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

    # Mirror bazel-bin symlink shape seen in real clones.
    (clone_dir / "bazel-bin").symlink_to(exec_bin, target_is_directory=True)

    # Create a broken blob symlink exactly in rules_oci format.
    (blob_dir / digest).symlink_to(
        Path("../../../../external/rules_oci++oci+debian_slim_linux_amd64/layout/blobs/sha256") / digest
    )
    blob_dir.chmod(0o555)
    fallback_blob = output_base / "external" / "rules_oci++oci+debian_slim_linux_amd64" / "blobs" / "sha256" / digest
    fallback_blob.parent.mkdir(parents=True, exist_ok=True)
    fallback_blob.write_text("rehydrated-blob")

    seen_layouts: list[Path] = []

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        layout = Path(args[-2])
        seen_layouts.append(layout)
        materialized_blob = layout / "blobs" / "sha256" / digest
        assert materialized_blob.exists()
        assert not materialized_blob.is_symlink()
        assert materialized_blob.read_text() == "rehydrated-blob"
        return _make_subprocess_result(
            stdout=f"{args[-1]}@sha256:{'a' * 64}\n",
            stderr="",
        )

    with patch("mlody.common.image_builder.phases.push.subprocess.run", side_effect=fake_run):
        result = push_image(clone_dir, "localhost:5000/mlody-test", ["my-tag"], _Auth())

    assert len(seen_layouts) == 1
    assert result.image_digest == f"sha256:{'a' * 64}"
    assert result.image_references == ["localhost:5000/mlody-test:my-tag"]


def test_push_image_includes_crane_output_in_push_error_context(tmp_path: Path) -> None:
    digest = "e" * 64
    clone_dir = tmp_path / "clone"
    image_layout = clone_dir / "bazel-bin" / "_dynamic_image" / "image"
    _write_minimal_layout(image_layout, digest)

    with patch(
        "mlody.common.image_builder.phases.push.subprocess.run",
        return_value=_make_subprocess_result(
            returncode=1,
            stdout="crane-stdout",
            stderr="crane-stderr",
        ),
    ):
        with pytest.raises(PushError) as exc_info:
            push_image(clone_dir, "localhost:5000/mlody-test", ["my-tag"], _Auth())

    assert exc_info.value.context["stderr"] == "crane-stderr"
    assert exc_info.value.context["stdout"] == "crane-stdout"
    assert exc_info.value.context["tag"] == "my-tag"


def test_push_image_repairs_blob_from_execroot_layout_fallback(tmp_path: Path) -> None:
    digest = "f" * 64
    output_base = tmp_path / "output-base"
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(parents=True)

    exec_bin = output_base / "execroot" / "_main" / "bazel-out" / "k8-fastbuild" / "bin"
    image_layout = exec_bin / "_dynamic_image" / "image"
    blob_dir = image_layout / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (image_layout / "index.json").write_text("{}")
    (image_layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

    (clone_dir / "bazel-bin").symlink_to(exec_bin, target_is_directory=True)

    # Broken link (relative to original rules_oci shape).
    (blob_dir / digest).symlink_to(
        Path("../../../../external/rules_oci++oci+debian_slim_linux_amd64/layout/blobs/sha256") / digest
    )
    # Blob is only available under execroot bazel-out external layout path.
    execroot_layout_blob = (
        output_base
        / "execroot"
        / "_main"
        / "bazel-out"
        / "k8-fastbuild"
        / "bin"
        / "external"
        / "rules_oci++oci+debian_slim_linux_amd64"
        / "layout"
        / "blobs"
        / "sha256"
        / digest
    )
    execroot_layout_blob.parent.mkdir(parents=True, exist_ok=True)
    execroot_layout_blob.write_text("execroot-layout-blob")

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        layout = Path(args[-2])
        materialized_blob = layout / "blobs" / "sha256" / digest
        assert materialized_blob.exists()
        assert not materialized_blob.is_symlink()
        assert materialized_blob.read_text() == "execroot-layout-blob"
        return _make_subprocess_result(stdout=f"{args[-1]}@sha256:{'b' * 64}\n", stderr="")

    with patch("mlody.common.image_builder.phases.push.subprocess.run", side_effect=fake_run):
        result = push_image(clone_dir, "localhost:5000/mlody-test", ["my-tag"], _Auth())

    assert result.image_digest == f"sha256:{'b' * 64}"


def test_push_image_repairs_blob_from_remote_cas_fallback(tmp_path: Path) -> None:
    digest = "1" * 64
    output_base = tmp_path / "bazel" / "_bazel_mav" / "abc123"
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(parents=True)

    exec_bin = output_base / "execroot" / "_main" / "bazel-out" / "k8-fastbuild" / "bin"
    image_layout = exec_bin / "_dynamic_image" / "image"
    blob_dir = image_layout / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (image_layout / "index.json").write_text("{}")
    (image_layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (clone_dir / "bazel-bin").symlink_to(exec_bin, target_is_directory=True)

    (blob_dir / digest).symlink_to(
        Path("../../../../external/rules_oci++oci+debian_slim_linux_amd64/layout/blobs/sha256") / digest
    )
    blob_dir.chmod(0o555)

    remote_cas_blob = tmp_path / "bazel" / "remote" / "cas" / digest[:2] / digest
    remote_cas_blob.parent.mkdir(parents=True, exist_ok=True)
    remote_cas_blob.write_text("remote-cas-blob")

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        layout = Path(args[-2])
        materialized_blob = layout / "blobs" / "sha256" / digest
        assert materialized_blob.exists()
        assert not materialized_blob.is_symlink()
        assert materialized_blob.read_text() == "remote-cas-blob"
        return _make_subprocess_result(stdout=f"{args[-1]}@sha256:{'c' * 64}\n", stderr="")

    with patch("mlody.common.image_builder.phases.push.subprocess.run", side_effect=fake_run):
        result = push_image(clone_dir, "localhost:5000/mlody-test", ["my-tag"], _Auth())

    assert result.image_digest == f"sha256:{'c' * 64}"
