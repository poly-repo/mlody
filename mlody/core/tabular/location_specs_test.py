"""Tests for typed location adapters and tabular-source factories."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from mlody.common.struct import Struct
from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata

from mlody.core.tabular import (
    CsvSource,
    DerivedLocationSpec,
    DerivedSource,
    MaterializedLocalSource,
    ParquetSource,
    PosixLocationSpec,
    RemoteLocationSpec,
    derived_location_spec_from_value,
    source_from_location,
    source_from_value,
)
from mlody.core.tabular.location_specs import query_rows_from_value


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    """Serve *tmp_path* over HTTP and return ``(base_url, root)``."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", tmp_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _remote_asset(path: Path, *, uri: str, content_hash: str) -> MaterializedAsset:
    return MaterializedAsset(
        path=path,
        content_hash=content_hash,
        metadata=AssetMetadata(
            uri=uri,
            resolved_url=uri,
            digest=None,
            digest_type=None,
            length=None,
            update_time=None,
            transport="http",
        ),
    )


def _manual() -> Struct:
    return Struct(kind="freshness", type="manual", name="manual", attributes={})


def _always() -> Struct:
    return Struct(kind="freshness", type="always", name="always", attributes={})


def test_posix_location_spec_reads_direct_path_field() -> None:
    location = Struct(kind="location", type="path", path="/tmp/data.parquet")

    spec = PosixLocationSpec.from_location(location)

    assert spec == PosixLocationSpec(paths=("/tmp/data.parquet",), name="", kind="path")


def test_posix_location_spec_reads_path_from_attributes() -> None:
    location = Struct(
        kind="location",
        type="posix",
        attributes={"path": ["data/train.parquet", "data/test.parquet"]},
    )

    spec = PosixLocationSpec.from_location(location)

    assert spec == PosixLocationSpec(
        paths=("data/train.parquet", "data/test.parquet"),
        name="",
        kind="posix",
    )


def test_derived_location_spec_from_value_uses_source_location_paths() -> None:
    value_struct = Struct(
        kind="value",
        name="derived",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":upstream",
                "sql_fragment": "WHERE split = 'train'",
                "dialect": "duckdb",
                "output_path": "/tmp/output.parquet",
            },
        ),
        source=Struct(
            kind="value",
            location=Struct(kind="location", type="path", path="data/*.parquet"),
        ),
    )

    spec = derived_location_spec_from_value(value_struct)

    assert spec == DerivedLocationSpec(
        source_ref=":upstream",
        source_paths=("data/*.parquet",),
        query=spec.query,
        output_path=Path("/tmp/output.parquet"),
        name="derived",
    )
    assert spec is not None
    assert spec.query.sql == "WHERE split = 'train'"


def test_source_from_location_returns_parquet_source_for_plain_location() -> None:
    source = source_from_location(Struct(kind="location", type="path", path="data.parquet"))

    assert isinstance(source, ParquetSource)
    assert source.paths == ("data.parquet",)


def test_source_from_value_returns_derived_source_for_derived_value() -> None:
    value_struct = Struct(
        kind="value",
        name="derived",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":upstream",
                "source_paths": ["data/*.parquet"],
                "sql_fragment": "WHERE score > 0.5",
                "dialect": "duckdb",
                "output_path": "/tmp/output.parquet",
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, DerivedSource)
    assert source.spec.source_paths == ("data/*.parquet",)


def test_remote_location_spec_reads_uri_from_attributes() -> None:
    location = Struct(
        kind="location",
        type="remote",
        attributes={"uri": "https://example.com/data.csv"},
    )

    spec = RemoteLocationSpec.from_location(location)

    assert spec == RemoteLocationSpec(uri="https://example.com/data.csv", name="remote")


def test_source_from_value_returns_csv_source_for_posix_csv_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(kind="location", type="posix", path="data.csv"),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, CsvSource)
    assert source.paths == ("data.csv",)


def test_query_rows_from_value_omits_header_row_for_csv_with_header(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(kind="location", type="posix", path=str(csv_path)),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    rows = query_rows_from_value(value_struct, "WHERE TRUE")

    assert rows == [
        {"name": "Alice", "salary": 120000},
        {"name": "Bob", "salary": 90000},
    ]


def test_source_from_value_returns_materialized_local_source_for_source_backed_csv_value(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "staged.csv"
    staged_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    destination_path = tmp_path / "cache" / "employees.csv"
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(
            staged_path,
            uri="https://example.com/employees.csv",
            content_hash="abc123",
        )
        source = source_from_value(value_struct)

        assert isinstance(source, MaterializedLocalSource)
        materialized = source.materialize()

    assert materialized == destination_path
    assert materialized.read_text() == staged_path.read_text()
    assert mock_materialize.call_count == 1


def test_source_from_value_returns_remote_csv_source_for_remote_csv_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        _lineage=[],
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.csv"},
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator="|",
            header_required=False,
            multifile=False,
            attributes={
                "separator": "|",
                "header_required": False,
                "multifile": False,
            },
        ),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(
            Path("/tmp/staged.csv"),
            uri="https://example.com/data.csv",
            content_hash="abc123",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, CsvSource)
    assert source.paths == ("/tmp/staged.csv",)
    assert source.separator == "|"
    assert source.header_required is False
    assert source.content_hash == "abc123"
    assert len(value_struct._lineage) == 1
    assert value_struct._lineage[0].source == "downloaded from"
    assert value_struct._lineage[0].new_value.data == "https://example.com/data.csv"
    assert value_struct._lineage[0].details == {
        "kind": "remote-download",
        "uri": "https://example.com/data.csv",
        "staged_path": "/tmp/staged.csv",
        "content_hash": "abc123",
        "location": {
            "kind": "location",
            "type": "remote",
            "attributes": {"uri": "https://example.com/data.csv"},
        },
    }


def test_source_from_value_returns_remote_parquet_source_for_remote_parquet_value() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.parquet"},
        ),
        representation=Struct(
            kind="representation",
            name="parquet",
            multifile=False,
            attributes={"multifile": False},
        ),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(
            Path("/tmp/staged.parquet"),
            uri="https://example.com/data.parquet",
            content_hash="def456",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, ParquetSource)
    assert source.paths == ("/tmp/staged.parquet",)
    assert source.content_hash == "def456"


def test_source_from_value_returns_none_for_unsupported_remote_representation() -> None:
    value_struct = Struct(
        kind="value",
        name="meta",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.json"},
        ),
        representation=Struct(
            kind="representation",
            name="json",
            attributes={},
        ),
    )

    assert source_from_value(value_struct) is None


def test_source_from_value_returns_none_for_remote_multifile_csv() -> None:
    value_struct = Struct(
        kind="value",
        name="employees",
        location=Struct(
            kind="location",
            type="remote",
            attributes={"uri": "https://example.com/data.csv"},
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=True,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": True,
            },
        ),
    )

    assert source_from_value(value_struct) is None


def test_source_from_value_builds_derived_source_for_remote_csv_source(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="high_paid",
        location=Struct(
            kind="location",
            type="derived",
            attributes={
                "source_ref": ":raw_employees",
                "sql_fragment": "WHERE salary > 100000",
                "dialect": "duckdb",
                "output_path": str(tmp_path / "derived.parquet"),
            },
        ),
        source=":raw_employees",
        _source_value=Struct(
            kind="value",
            name="raw_employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        mock_materialize.return_value = _remote_asset(
            csv_path,
            uri="https://example.com/employees.csv",
            content_hash="hash123",
        )
        source = source_from_value(value_struct)

    assert isinstance(source, DerivedSource)
    materialized = source.materialize()
    assert materialized.exists()
    assert pq.read_table(materialized).column("name").to_pylist() == ["Alice"]


def test_source_backed_local_source_expands_home_and_reuses_cache(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "employees.csv"
    staged_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="employees_local",
        _lineage=[],
        location=Struct(
            kind="location",
            type="posix",
            path="~/.cache/mlody/artifacts/employees.csv",
        ),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            _lineage=[],
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with patch.dict("os.environ", {"HOME": str(tmp_path)}):
        with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
            mock_materialize.return_value = _remote_asset(
                staged_path,
                uri="https://example.com/employees.csv",
                content_hash="abc123",
            )
            source = source_from_value(value_struct)

            assert isinstance(source, MaterializedLocalSource)
            first = source.materialize()
            second = source.materialize()

    assert first == tmp_path / ".cache" / "mlody" / "artifacts" / "employees.csv"
    assert second == first
    assert first.read_text() == staged_path.read_text()
    assert mock_materialize.call_count == 1
    assert [event.source for event in value_struct._lineage] == [
        "downloaded from",
        "copied from",
    ]
    assert value_struct._lineage[0].new_value.data == "https://example.com/employees.csv"
    assert value_struct._lineage[0].details == {
        "kind": "remote-download",
        "uri": "https://example.com/employees.csv",
        "staged_path": str(staged_path),
        "content_hash": "abc123",
        "location": {
            "kind": "location",
            "type": "remote",
            "attributes": {"uri": "https://example.com/employees.csv"},
        },
    }
    assert value_struct._lineage[1].new_value.data == ":employees"
    assert value_struct._lineage[1].details == {
        "kind": "local-copy",
        "source_label": ":employees",
        "source_path": str(staged_path),
        "destination_path": str(first),
    }
    source_lineage = value_struct._source_value._lineage
    assert len(source_lineage) == 1
    assert source_lineage[0].source == "downloaded from"
    assert source_lineage[0].new_value.data == "https://example.com/employees.csv"


def test_source_backed_local_source_cache_hit_reconstructs_upstream_lineage(
    tmp_path: Path,
) -> None:
    destination_path = tmp_path / "cache" / "employees.csv"
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    value_struct = Struct(
        kind="value",
        name="employees_local",
        _lineage=[],
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            _lineage=[],
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
        source = source_from_value(value_struct)

        assert isinstance(source, MaterializedLocalSource)
        materialized = source.materialize()

    assert materialized == destination_path
    assert mock_materialize.call_count == 0
    assert [event.source for event in value_struct._lineage] == [
        "downloaded from",
        "copied from",
    ]
    assert value_struct._lineage[0].new_value.data == "https://example.com/employees.csv"
    assert value_struct._lineage[0].details == {
        "kind": "remote-download",
        "uri": "https://example.com/employees.csv",
        "location": {
            "kind": "location",
            "type": "remote",
            "attributes": {"uri": "https://example.com/employees.csv"},
        },
    }
    assert value_struct._lineage[1].details == {
        "kind": "local-copy",
        "source_label": ":employees",
        "source_path": None,
        "destination_path": str(destination_path),
    }


def test_source_backed_local_source_revalidates_remote_for_always_freshness(
    http_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, root = http_server
    source_path = root / "employees.csv"
    source_path.write_text("name,salary\nAlice,120000\n")
    destination_path = tmp_path / "cache" / "employees.csv"
    value_struct = Struct(
        kind="value",
        name="employees_local",
        freshness=_always(),
        location=Struct(kind="location", type="posix", path=str(destination_path)),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            freshness=_manual(),
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": f"{base_url}/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, MaterializedLocalSource)
    first = source.materialize()
    source_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
    second = source.materialize()

    assert first == second == destination_path
    assert destination_path.read_text() == source_path.read_text()


def test_source_backed_local_source_raises_for_non_tabular_source() -> None:
    destination_path = "/tmp/employees.csv"
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(kind="location", type="posix", path=destination_path),
        source=":meta",
        _source_value=Struct(
            kind="value",
            name="meta",
            location=Struct(kind="location", type="inline"),
            representation=Struct(kind="representation", name="json", attributes={}),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    source = source_from_value(value_struct)

    assert isinstance(source, MaterializedLocalSource)
    with pytest.raises(ValueError, match="non-tabular source 'meta'"):
        source.materialize()


def test_source_backed_local_source_raises_for_multiple_destination_paths() -> None:
    value_struct = Struct(
        kind="value",
        name="employees_local",
        location=Struct(
            kind="location",
            type="posix",
            path=["cache/one.csv", "cache/two.csv"],
        ),
        source=":employees",
        _source_value=Struct(
            kind="value",
            name="employees",
            location=Struct(
                kind="location",
                type="remote",
                attributes={"uri": "https://example.com/employees.csv"},
            ),
            representation=Struct(
                kind="representation",
                name="csv",
                separator=",",
                header_required=True,
                multifile=False,
                attributes={
                    "separator": ",",
                    "header_required": True,
                    "multifile": False,
                },
            ),
        ),
        representation=Struct(
            kind="representation",
            name="csv",
            separator=",",
            header_required=True,
            multifile=False,
            attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
        ),
    )

    with pytest.raises(ValueError, match="exactly one destination path"):
        source_from_value(value_struct)
