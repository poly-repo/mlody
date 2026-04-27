import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from mlody.common.huggingface import cli as cli_module
from mlody.common.huggingface import download as download_module
from mlody.common.huggingface import repo_client as repo_client_module
from mlody.common.huggingface import repo_types as repo_types_module
from mlody.common.huggingface import resume_state as resume_state_module


def _load_entrypoint_module():
    module_path = Path(__file__).with_name("model-download.py")
    spec = importlib.util.spec_from_file_location("model_download", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self._chunks


def test_download_segment_raises_when_range_request_is_not_partial(
    monkeypatch, tmp_path
):
    destination = tmp_path / "weights.bin"
    destination.write_bytes(b"\0" * 8)
    request_kwargs = {}

    def fake_get(url, **kwargs):
        del url
        request_kwargs.update(kwargs)
        return _FakeResponse(status_code=200, chunks=[b"abcd"])

    monkeypatch.setattr(download_module.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="returned 200 instead of 206"):
        download_module.download_segment(
            "https://example.invalid/model",
            0,
            3,
            destination,
            None,
        )

    assert request_kwargs["timeout"] == download_module.REQUEST_TIMEOUT


def test_segmented_download_follows_redirects_for_head(monkeypatch, tmp_path):
    destination = tmp_path / "weights.bin"
    head_kwargs = {}

    def fake_head(url, **kwargs):
        del url
        head_kwargs.update(kwargs)
        return _FakeResponse(headers={"Content-Length": "4"})

    def fake_download_segment(url, start, end, path, token):
        del url, token
        with open(path, "r+b") as handle:
            handle.seek(start)
            handle.write(b"x" * (end - start + 1))

    monkeypatch.setattr(download_module.requests, "head", fake_head)
    monkeypatch.setattr(download_module, "download_segment", fake_download_segment)

    download_module.segmented_download(
        "https://example.invalid/model",
        destination,
        token=None,
        workers=1,
    )

    assert head_kwargs["allow_redirects"] is True
    assert head_kwargs["timeout"] == download_module.REQUEST_TIMEOUT
    assert destination.read_bytes() == b"xxxx"


def test_download_file_ignores_same_size_partial_and_replaces_final(
    monkeypatch, tmp_path
):
    destination_dir = tmp_path / "cache"
    destination_dir.mkdir()
    final_path = destination_dir / "weights.bin"
    partial_file = destination_dir / "weights.bin.partial"
    partial_file.write_bytes(b"\0" * 4)
    calls = {"segmented": 0}

    def fake_build_file_url(repo_id, revision, file_path, repo_type="model"):
        del repo_id, revision, file_path, repo_type
        return "https://example.invalid/model-file"

    def fake_head(url, **kwargs):
        del url, kwargs
        return _FakeResponse(
            headers={"Content-Length": str(300 * 1024 * 1024)}
        )

    def fake_segmented_download(url, dest, token, workers):
        del url, token, workers
        calls["segmented"] += 1
        assert dest == partial_file
        dest.write_bytes(b"done")

    monkeypatch.setattr(download_module, "build_file_url", fake_build_file_url)
    monkeypatch.setattr(download_module.requests, "head", fake_head)
    monkeypatch.setattr(
        download_module,
        "segmented_download",
        fake_segmented_download,
    )

    download_module.download_file(
        "google/gemma",
        "main",
        "weights.bin",
        destination_dir,
        token=None,
        workers=1,
    )

    assert calls["segmented"] == 1
    assert final_path.read_bytes() == b"done"
    assert not partial_file.exists()


def test_segmented_download_resumes_only_incomplete_segments(monkeypatch, tmp_path):
    destination = tmp_path / "weights.bin.partial"
    metadata_path = tmp_path / "weights.bin.partial.metadata.json"
    original_segment_size = download_module.SEGMENT_SIZE
    head_kwargs = {}
    downloaded = []

    monkeypatch.setattr(download_module, "SEGMENT_SIZE", 4)

    def fake_head(url, **kwargs):
        del url
        head_kwargs.update(kwargs)
        return _FakeResponse(headers={"Content-Length": "12"})

    def fake_download_segment(url, start, end, path, token):
        del url, token
        downloaded.append((start, end))
        with open(path, "r+b") as handle:
            handle.seek(start)
            handle.write(bytes([65 + (start // 4)]) * (end - start + 1))

    destination.write_bytes(b"AAAA\0\0\0\0\0\0\0\0")
    metadata_path.write_text(
        '{"size": 12, "segment_size": 4, "segment_count": 3, "completed_segments": [true, false, false]}'
    )

    monkeypatch.setattr(download_module.requests, "head", fake_head)
    monkeypatch.setattr(download_module, "download_segment", fake_download_segment)

    try:
        download_module.segmented_download(
            "https://example.invalid/model",
            destination,
            token=None,
            workers=1,
        )

        assert head_kwargs["allow_redirects"] is True
        assert downloaded == [(4, 7), (8, 11)]
        assert destination.read_bytes() == b"AAAABBBBCCCC"
        metadata = resume_state_module.load_partial_metadata(
            destination,
            12,
            3,
            segment_size=4,
        )
        assert metadata is not None
        assert metadata.completed_segments == (True, True, True)
    finally:
        monkeypatch.setattr(download_module, "SEGMENT_SIZE", original_segment_size)


def test_download_file_uses_dataset_repo_type_in_build_file_url(
    monkeypatch, tmp_path
):
    destination_dir = tmp_path / "cache"
    destination_dir.mkdir()
    final_path = destination_dir / "train" / "data.parquet"
    captured = {}
    head_urls = []

    def fake_build_file_url(repo_id, revision, file_path, repo_type="model"):
        captured.update(
            {
                "repo_id": repo_id,
                "revision": revision,
                "file_path": file_path,
                "repo_type": repo_type,
            }
        )
        return "https://example.invalid/dataset-file"

    def fake_head(url, **kwargs):
        del kwargs
        head_urls.append(url)
        return _FakeResponse(headers={"Content-Length": "4"})

    def fake_get(url, **kwargs):
        del url, kwargs
        return _FakeResponse(chunks=[b"data"])

    monkeypatch.setattr(download_module, "build_file_url", fake_build_file_url)
    monkeypatch.setattr(download_module.requests, "head", fake_head)
    monkeypatch.setattr(download_module.requests, "get", fake_get)

    download_module.download_file(
        "bigcode/the-stack",
        "main",
        "train/data.parquet",
        destination_dir,
        token=None,
        workers=1,
        repo_type=repo_types_module.RepoType.DATASET,
    )

    assert captured["repo_id"] == "bigcode/the-stack"
    assert captured["file_path"] == "train/data.parquet"
    assert captured["revision"] == "main"
    assert captured["repo_type"] == repo_types_module.RepoType.DATASET
    assert head_urls == ["https://example.invalid/dataset-file"]
    assert final_path.read_bytes() == b"data"


def test_list_tags_and_refs_forward_dataset_repo_type(monkeypatch):
    calls = []

    class _FakeApi:
        def __init__(self, token):
            assert token == "hf-token"

        def list_repo_refs(self, repo_id, repo_type):
            calls.append((repo_id, repo_type))
            return SimpleNamespace(
                branches=[SimpleNamespace(name="main", target_commit="branch-sha")],
                tags=[SimpleNamespace(name="v1.0", target_commit="tag-sha")],
            )

    monkeypatch.setattr(repo_client_module, "HfApi", _FakeApi)

    repo_client_module.list_tags(
        "bigcode/the-stack",
        "hf-token",
        repo_type=repo_types_module.RepoType.DATASET,
    )
    repo_client_module.list_refs(
        "bigcode/the-stack",
        "hf-token",
        repo_type=repo_types_module.RepoType.DATASET,
    )

    assert calls == [
        ("bigcode/the-stack", "dataset"),
        ("bigcode/the-stack", "dataset"),
    ]


def test_fetch_repo_snapshot_dispatches_to_dataset_info(monkeypatch):
    calls = {}
    fake_info = SimpleNamespace(
        sha="dataset-sha",
        siblings=[SimpleNamespace(rfilename="train/data.parquet")],
    )

    def fake_dataset_info(repo, revision=None, token=None):
        calls["dataset_info"] = (repo, revision, token)
        return fake_info

    def fake_model_info(*args, **kwargs):
        del args, kwargs
        raise AssertionError("model_info should not be used for dataset repos")

    monkeypatch.setattr(repo_client_module, "dataset_info", fake_dataset_info)
    monkeypatch.setattr(repo_client_module, "model_info", fake_model_info)

    snapshot = repo_client_module.fetch_repo_snapshot(
        "bigcode/the-stack",
        "main",
        "hf-token",
        repo_type=repo_types_module.RepoType.DATASET,
    )

    assert calls["dataset_info"] == ("bigcode/the-stack", "main", "hf-token")
    assert snapshot.metadata_filename == "dataset_info.json"
    assert snapshot.files == ("train/data.parquet",)


def test_load_partial_metadata_rejects_invalid_completed_segments(tmp_path):
    partial_file = tmp_path / "weights.bin.partial"
    partial_file.write_bytes(b"\0" * 8)
    metadata_path = resume_state_module.partial_metadata_path(partial_file)
    metadata_path.write_text(
        '{"size": 8, "segment_size": 4, "segment_count": 2, "completed_segments": [true, "bad"]}'
    )

    metadata = resume_state_module.load_partial_metadata(
        partial_file,
        8,
        2,
        segment_size=4,
    )

    assert metadata is None


def test_repo_type_dataset_uses_dataset_cache_root():
    base_out = repo_types_module.RepoType.DATASET.default_base_out(
        "bigcode/the-stack"
    )

    assert str(base_out).endswith(
        ".cache/mlody/artifacts/huggingface/datasets/bigcode/the-stack"
    )
    assert repo_types_module.RepoType.DATASET.metadata_filename == "dataset_info.json"


def test_entrypoint_main_download_dataset_uses_dataset_cache_root(
    monkeypatch, tmp_path
):
    entrypoint = _load_entrypoint_module()
    captured = {}
    fake_info = SimpleNamespace(
        sha="dataset-sha",
        siblings=[SimpleNamespace(rfilename="train/data.parquet")],
    )
    snapshot = repo_client_module.RepoSnapshot(
        repo_id="bigcode/the-stack",
        requested_revision=None,
        repo_type=repo_types_module.RepoType.DATASET,
        info=fake_info,
    )

    def fake_fetch_repo_snapshot(repo, revision, token, repo_type="model"):
        captured["fetch_repo_snapshot"] = (repo, revision, token, repo_type)
        return snapshot

    def fake_download_repo(
        repo, revision, dest, files, workers, token, repo_type="model", print_fn=print
    ):
        del print_fn
        captured["download_repo"] = {
            "repo": repo,
            "revision": revision,
            "dest": dest,
            "files": files,
            "workers": workers,
            "token": token,
            "repo_type": repo_type,
        }

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "hf-token")
    monkeypatch.setattr(cli_module, "fetch_repo_snapshot", fake_fetch_repo_snapshot)
    monkeypatch.setattr(cli_module, "download_repo", fake_download_repo)
    monkeypatch.setattr(
        cli_module.sys,
        "argv",
        [
            "model-download.py",
            "download",
            "--dataset",
            "bigcode/the-stack",
            "-w",
            "1",
        ],
    )

    entrypoint.main()

    expected_dir = (
        tmp_path
        / ".cache"
        / "mlody"
        / "artifacts"
        / "huggingface"
        / "datasets"
        / "bigcode"
        / "the-stack"
        / "dataset-sha"
    )
    assert captured["fetch_repo_snapshot"] == (
        "bigcode/the-stack",
        None,
        "hf-token",
        repo_types_module.RepoType.DATASET,
    )
    assert captured["download_repo"]["dest"] == expected_dir
    assert captured["download_repo"]["repo_type"] == repo_types_module.RepoType.DATASET
    assert (expected_dir / "dataset_info.json").exists()


def test_entrypoint_main_backward_compatibility_defaults_to_model_repo(
    monkeypatch, tmp_path
):
    entrypoint = _load_entrypoint_module()
    captured = {}
    fake_info = SimpleNamespace(
        sha="model-sha",
        siblings=[SimpleNamespace(rfilename="config.json")],
    )
    snapshot = repo_client_module.RepoSnapshot(
        repo_id="google/gemma",
        requested_revision=None,
        repo_type=repo_types_module.RepoType.MODEL,
        info=fake_info,
    )

    def fake_fetch_repo_snapshot(repo, revision, token, repo_type="model"):
        captured["fetch_repo_snapshot"] = (repo, revision, token, repo_type)
        return snapshot

    def fake_download_repo(
        repo, revision, dest, files, workers, token, repo_type="model", print_fn=print
    ):
        del print_fn
        captured["download_repo"] = {
            "repo": repo,
            "revision": revision,
            "dest": dest,
            "files": files,
            "workers": workers,
            "token": token,
            "repo_type": repo_type,
        }

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "hf-token")
    monkeypatch.setattr(cli_module, "fetch_repo_snapshot", fake_fetch_repo_snapshot)
    monkeypatch.setattr(cli_module, "download_repo", fake_download_repo)
    monkeypatch.setattr(
        cli_module.sys,
        "argv",
        [
            "model-download.py",
            "google/gemma",
            "-w",
            "1",
        ],
    )

    entrypoint.main()

    expected_dir = (
        tmp_path
        / ".cache"
        / "mlody"
        / "artifacts"
        / "huggingface"
        / "google"
        / "gemma"
        / "model-sha"
    )
    assert captured["fetch_repo_snapshot"] == (
        "google/gemma",
        None,
        "hf-token",
        repo_types_module.RepoType.MODEL,
    )
    assert captured["download_repo"]["dest"] == expected_dir
    assert captured["download_repo"]["repo_type"] == repo_types_module.RepoType.MODEL
    assert (expected_dir / "model_info.json").exists()
