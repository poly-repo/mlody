"""Tests for the mlody content-hash generic and Python helper."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
from common.python.starlarkish.core.struct import Struct

from mlody.common.hash import hash as value_hash
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


def test_hash_generic_returns_upstream_remote_hash_for_source_backed_local_value() -> None:
    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="source-remote-hash")

        ev = _eval(
            """
            cached_value(
                name="artifact",
                type=string(),
                source=remote(uri="https://example.com/data.txt"),
                location=posix(path="/tmp/artifact.txt"),
                freshness=ttl(duration="P1D"),
            )
            result = hash(builtins.lookup("value", "artifact"))
            """
        )

    assert _result(ev) == "source-remote-hash"
    mock_materialize.assert_called_once()


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


def test_python_hash_returns_upstream_remote_hash_for_source_backed_local_value() -> None:
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
    local_value = Struct(
        kind="value",
        name="artifact",
        location=Struct(
            kind="location",
            type="posix",
            name="posix",
            attributes={"path": ["/tmp/artifact.txt"]},
        ),
        source=":artifact-remote",
        _source_value=remote_value,
        freshness=Struct(kind="freshness", type="ttl", name="ttl", attributes={"duration": "P1D"}),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(content_hash="py-source-remote-hash")
        result = value_hash(local_value)

    assert result == "py-source-remote-hash"
    mock_materialize.assert_called_once()


def test_python_hash_returns_none_for_non_remote_values_and_entities() -> None:
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
    task = Struct(kind="task", name="trainer")

    assert value_hash(local_value) is None
    assert value_hash(task) is None
