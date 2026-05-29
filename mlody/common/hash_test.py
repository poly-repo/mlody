"""Tests for the mlody content-hash generic and Python helper."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
from common.python.starlarkish.core.struct import Struct

from mlody.common.hash import hash as value_hash
from mlody.core.dag import PortRef
from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_FRESHNESS_MLODY = (_THIS_DIR / "freshness.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()
_MM_MLODY = (_THIS_DIR / "mm.mlody").read_text()
_HASH_MLODY = (_THIS_DIR / "hash.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/freshness.mlody": _FRESHNESS_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/mm.mlody": _MM_MLODY,
    "mlody/common/hash.mlody": _HASH_MLODY,
}


def _metadata(uri: str = "https://example.com/data.txt") -> AssetMetadata:
    return AssetMetadata(
        uri=uri,
        resolved_url=uri,
        digest=None,
        digest_type=None,
        length=None,
        update_time=None,
    )


def _remote_asset(
    path: str = "/tmp/data.txt",
    *,
    uri: str = "https://example.com/data.txt",
    content_hash: str = "abc123",
) -> MaterializedAsset:
    return MaterializedAsset(
        path=Path(path),
        content_hash=content_hash,
        metadata=_metadata(uri),
    )


def _write_ssh_cache(
    tmp_path: Path,
    monkeypatch,
    *,
    host: str = "hooli",
    remote_path: str = "/exports/data.txt",
    contents: str = "ssh payload\n",
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    relative = remote_path[1:] if remote_path.startswith("/") else remote_path
    cache_path = tmp_path / ".cache" / "mlody" / "remotes" / host / relative
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(contents)
    return cache_path


def _write_local_artifact(
    path: Path,
    *,
    contents: str,
    age_seconds: float | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    if age_seconds is not None:
        timestamp = time.time() - age_seconds
        os.utime(path, (timestamp, timestamp))
    return path


def _eval(extra_mlody: str) -> Evaluator:
    script = (
        'load("//mlody/common/types.mlody")\n'
        'load("//mlody/common/freshness.mlody")\n'
        'load("//mlody/common/locations.mlody")\n'
        'load("//mlody/common/values.mlody")\n'
        + dedent(extra_mlody)
    )
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        mm_path = root / "mlody" / "common" / "mm.mlody"
        ev.eval_file(mm_path)
        mm_globals = ev._module_globals.get(mm_path, {})
        for name in ("mm", "defmethod"):
            if name in mm_globals:
                ev._persistent_injections[name] = mm_globals[name]

        hash_path = root / "mlody" / "common" / "hash.mlody"
        ev.eval_file(hash_path)
        hash_globals = ev._module_globals.get(hash_path, {})
        if "hash" in hash_globals:
            ev._persistent_injections["hash"] = hash_globals["hash"]

        ev.eval_file(root / "test.mlody")
    return ev


def _result(ev: Evaluator) -> object:
    return ev._module_globals[ev.root_path / "test.mlody"]["result"]


def test_hash_generic_materializes_remote_value_and_returns_content_hash() -> None:
    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="remote-hash-1")

        ev = _eval(
            """
            artifact = value(
                name="artifact",
                type=string(),
                location=remote(uri="https://example.com/data.txt"),
                freshness=always(),
            )
            result = hash(artifact)
            """
        )

    assert _result(ev) == "remote-hash-1"
    mock_materialize.assert_called_once()


def test_hash_generic_returns_none_for_non_remote_value() -> None:
    ev = _eval(
        """
        artifact = value(
            name="artifact",
            type=string(),
            location=posix(path="/tmp/data.txt"),
        )
        result = hash(artifact)
        """
    )

    assert _result(ev) is None


def test_hash_generic_returns_content_hash_for_ssh_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contents = "ssh payload\n"
    _write_ssh_cache(
        tmp_path,
        monkeypatch,
        host="hooli",
        remote_path="/exports/data.txt",
        contents=contents,
    )

    ev = _eval(
        """
        artifact = value(
            name="artifact",
            type=string(),
            location=ssh(host="hooli", path="/exports/data.txt"),
            freshness=manual(),
        )
        result = hash(artifact)
        """
    )

    assert _result(ev) == hashlib.sha256(contents.encode("utf-8")).hexdigest()


def test_hash_generic_returns_cached_local_hash_for_fresh_source_backed_remote_value(
    tmp_path: Path,
) -> None:
    destination = _write_local_artifact(
        tmp_path / "artifact.txt",
        contents="cached local payload\n",
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="source-remote-hash")

        ev = _eval(
            f"""
            cached_value(
                name="artifact",
                type=string(),
                source=remote(uri="https://example.com/data.txt"),
                location=posix(path="{destination}"),
                freshness=ttl(duration="P1D"),
            )
            result = hash(builtins.lookup("value", "artifact"))
            """
        )

    assert _result(ev) == hashlib.sha256(b"cached local payload\n").hexdigest()
    mock_materialize.assert_not_called()


def test_hash_generic_returns_cached_local_hash_for_fresh_source_backed_ssh_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote_contents = "ssh payload\n"
    _write_ssh_cache(
        tmp_path,
        monkeypatch,
        host="hooli",
        remote_path="/exports/data.txt",
        contents=remote_contents,
    )
    destination = _write_local_artifact(
        tmp_path / "artifact.txt",
        contents="cached local payload\n",
    )

    ev = _eval(
        f"""
        cached_value(
            name="artifact",
            type=string(),
            source=ssh(host="hooli", path="/exports/data.txt"),
            location=posix(path="{destination}"),
            freshness=ttl(duration="P1D"),
        )
        result = hash(builtins.lookup("value", "artifact"))
        """
    )

    assert _result(ev) == hashlib.sha256(b"cached local payload\n").hexdigest()
    assert destination.read_text() == "cached local payload\n"


def test_python_hash_refreshes_source_backed_ssh_value_when_freshness_due(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote_contents = "ssh payload\n"
    _write_ssh_cache(
        tmp_path,
        monkeypatch,
        host="hooli",
        remote_path="/exports/data.txt",
        contents=remote_contents,
    )
    destination = _write_local_artifact(
        tmp_path / "artifact.txt",
        contents="stale local payload\n",
        age_seconds=2 * 24 * 60 * 60,
    )
    remote_value = Struct(
        kind="value",
        name="artifact-remote",
        location=Struct(
            kind="location",
            type="ssh",
            name="ssh",
            attributes={"host": "hooli", "path": "/exports/data.txt"},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )
    local_value = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": [str(destination)]},
        ),
        source=":artifact-remote",
        _source_value=remote_value,
        freshness=Struct(kind="freshness", type="ttl", name="ttl", attributes={"duration": "P1D"}),
    )

    assert value_hash(local_value) == hashlib.sha256(remote_contents.encode("utf-8")).hexdigest()
    assert destination.read_text() == remote_contents


def test_python_hash_returns_remote_content_hash() -> None:
    value_struct = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="remote",
            name="remote",
            attributes={"uri": "https://example.com/data.txt"},
        ),
        freshness=Struct(kind="freshness", type="always", name="always", attributes={}),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="py-hash-1")
        result = value_hash(value_struct)

    assert result == "py-hash-1"
    mock_materialize.assert_called_once()


def test_python_hash_returns_content_hash_for_ssh_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contents = "ssh payload\n"
    _write_ssh_cache(
        tmp_path,
        monkeypatch,
        host="hooli",
        remote_path="/exports/data.txt",
        contents=contents,
    )
    value_struct = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="ssh",
            name="ssh",
            attributes={"host": "hooli", "path": "/exports/data.txt"},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )

    result = value_hash(value_struct)

    assert result == hashlib.sha256(contents.encode("utf-8")).hexdigest()


def test_python_hash_returns_cached_local_hash_for_fresh_source_backed_remote_value(
    tmp_path: Path,
) -> None:
    remote_value = Struct(
        kind="value",
        name="artifact-remote",
        location=Struct(
            kind="location",
            type="remote",
            name="remote",
            attributes={"uri": "https://example.com/data.txt"},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )
    destination = _write_local_artifact(
        tmp_path / "artifact.txt",
        contents="cached local payload\n",
    )
    local_value = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": [str(destination)]},
        ),
        source=":artifact-remote",
        _source_value=remote_value,
        freshness=Struct(kind="freshness", type="ttl", name="ttl", attributes={"duration": "P1D"}),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="py-source-remote-hash")
        result = value_hash(local_value)

    assert result == hashlib.sha256(b"cached local payload\n").hexdigest()
    mock_materialize.assert_not_called()


def test_python_hash_returns_cached_local_hash_for_source_backed_local_value(
    tmp_path: Path,
) -> None:
    source_path = _write_local_artifact(
        tmp_path / "source.txt",
        contents="shared local payload\n",
    )
    destination = _write_local_artifact(
        tmp_path / "artifact.txt",
        contents="shared local payload\n",
    )
    source_value = Struct(
        kind="value",
        name="artifact-source",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": [str(source_path)]},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )
    local_value = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": [str(destination)]},
        ),
        source=":artifact-source",
        _source_value=source_value,
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )

    assert value_hash(local_value) == hashlib.sha256(b"shared local payload\n").hexdigest()


def test_python_hash_returns_payload_hash_for_inline_data_value() -> None:
    value_struct = Struct(
        kind="value",
        name="run_config",
        location=Struct(
            kind="location",
            type="inline",
            data=Struct(batch_size=32, enabled=True),
        ),
    )

    expected = hashlib.sha256(
        b'{"batch_size":32,"enabled":true}'
    ).hexdigest()

    assert value_hash(value_struct) == expected


def test_python_hash_returns_none_for_plain_local_values() -> None:
    local_value = Struct(
        kind="value",
        name="local",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": ["/tmp/local.txt"]},
        ),
    )

    assert value_hash(local_value) is None


def test_python_hash_returns_deterministic_task_hash() -> None:
    task = Struct(
        kind="task",
        name="trainer",
        inputs={
            "dataset": Struct(
                kind="value",
                name="dataset",
                type=Struct(kind="type", type="string", name="string"),
                location=Struct(
                    kind="location",
                    type="inline",
                    name="inline",
                    data=Struct(uri="s3://datasets/train.csv"),
                ),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        },
        config={
            "batch_size": Struct(
                kind="value",
                name="batch_size",
                type=Struct(kind="type", type="integer", name="integer"),
                location=Struct(
                    kind="location",
                    type="inline",
                    name="inline",
                    data=Struct(value=32),
                ),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        },
    )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(task)
        second = value_hash(task)

    assert isinstance(first, str)
    assert first == second


def test_python_hash_of_task_includes_task_identity() -> None:
    base_task = Struct(
        kind="task",
        name="trainer",
        _source_range=Struct(
            kind="mlody-source-range",
            filepath="/repo/pipeline.mlody",
            start_line=10,
            end_line=14,
        ),
        inputs={},
        config={},
    )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(base_task)
        second = value_hash(base_task.updated(name="evaluator"))

    assert first != second


def test_python_hash_of_task_changes_when_config_changes() -> None:
    def _task(batch_size: int) -> Struct:
        return Struct(
            kind="task",
            name="trainer",
            inputs={},
            config={
                "batch_size": Struct(
                    kind="value",
                    name="batch_size",
                    type=Struct(kind="type", type="integer", name="integer"),
                    location=Struct(
                        kind="location",
                        type="inline",
                        name="inline",
                        data=Struct(value=batch_size),
                    ),
                    freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
                )
            },
        )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(_task(32))
        second = value_hash(_task(64))

    assert first != second


def test_python_hash_of_task_sorts_input_and_config_names() -> None:
    task_a = Struct(
        kind="task",
        name="trainer",
        inputs={
            "b": Struct(
                kind="value",
                name="b",
                type=Struct(kind="type", type="string", name="string"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value="B")),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            ),
            "a": Struct(
                kind="value",
                name="a",
                type=Struct(kind="type", type="string", name="string"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value="A")),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            ),
        },
        config={},
    )
    task_b = Struct(
        kind="task",
        name="trainer",
        inputs={
            "a": task_a.inputs["a"],
            "b": task_a.inputs["b"],
        },
        config={},
    )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(task_a)
        second = value_hash(task_b)

    assert first == second


def test_python_hash_of_task_follows_producer_task_hash_transitively() -> None:
    producer_output = Struct(
        kind="value",
        name="model",
        type=Struct(kind="type", type="string", name="string"),
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": ["/tmp/model.bin"]},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )
    producer_a = Struct(
        kind="task",
        name="producer",
        inputs={},
        config={
            "epochs": Struct(
                kind="value",
                name="epochs",
                type=Struct(kind="type", type="integer", name="integer"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value=3)),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        },
    )
    producer_b = producer_a.updated(
        config={
            "epochs": Struct(
                kind="value",
                name="epochs",
                type=Struct(kind="type", type="integer", name="integer"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value=5)),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        }
    )
    consumer_a = Struct(
        kind="task",
        name="consumer",
        inputs={
            "model": Struct(
                kind="value",
                name="model",
                type=Struct(kind="type", type="string", name="string"),
                location=Struct(
                    kind="location",
                    type="posix",
                    name="posix",
                    attributes={"path": ["/tmp/model.bin"]},
                ),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
                source=PortRef(task="producer", port="model"),
                _source_value=producer_output.updated(_producer_task=producer_a),
            )
        },
        config={},
    )
    consumer_b = consumer_a.updated(
        inputs={
            "model": consumer_a.inputs["model"].updated(
                _source_value=producer_output.updated(_producer_task=producer_b)
            )
        }
    )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(consumer_a)
        second = value_hash(consumer_b)

    assert first != second


def test_python_hash_of_produced_output_uses_producer_task_hash() -> None:
    producer_output = Struct(
        kind="value",
        name="model",
        type=Struct(kind="type", type="string", name="string"),
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": ["/tmp/model.bin"]},
        ),
        freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
    )
    producer_a = Struct(
        kind="task",
        name="producer",
        inputs={},
        config={
            "epochs": Struct(
                kind="value",
                name="epochs",
                type=Struct(kind="type", type="integer", name="integer"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value=3)),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        },
    )
    producer_b = producer_a.updated(
        config={
            "epochs": Struct(
                kind="value",
                name="epochs",
                type=Struct(kind="type", type="integer", name="integer"),
                location=Struct(kind="location", type="inline", name="inline", data=Struct(value=5)),
                freshness=Struct(kind="freshness", type="manual", name="manual", attributes={}),
            )
        }
    )

    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        first = value_hash(producer_output.updated(_producer_task=producer_a))
        second = value_hash(producer_output.updated(_producer_task=producer_b))

    assert isinstance(first, str)
    assert len(first) == 64
    assert first != second


def test_hash_generic_returns_task_hash_in_mlody() -> None:
    with patch("mlody.common.hash._task_base_hash", return_value="task-base-hash"):
        ev = _eval(
            """
            trainer = struct(kind="task", name="trainer", inputs=[], config={})
            result = hash(trainer)
            """
        )

    result = _result(ev)
    assert isinstance(result, str)
    assert len(result) == 64
