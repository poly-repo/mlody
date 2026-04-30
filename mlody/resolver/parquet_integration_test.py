"""Integration tests for Parquet-backed label resolution.

Tests end-to-end: real Parquet file on disk → resolve_label_to_value →
typed MlodyValue result.  Uses pytest's ``tmp_path`` fixture (real filesystem)
because pyarrow cannot write to pyfakefs.

Covers:
- TEST-P-006: end-to-end index access
- TEST-P-006: end-to-end index + field chained access
- TEST-P-007: regression — non-Parquet kind="value" labels unchanged
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mlody.core.label import parse_label
from mlody.core.workspace import Workspace
from mlody.resolver.label_value import (
    MlodyUnresolvedValue,
    MlodyValueValue,
    _RawAttrValue,
    resolve_label_to_value,
)

# Real source files required by workspace_loader Phase 1.
_REAL_RULE_MLODY = Path(__file__).parent.parent / "core" / "rule.mlody"
_REAL_MM_MLODY = Path(__file__).parent.parent / "common" / "mm.mlody"


def _add_mm_files(root: Path) -> None:
    """Copy rule.mlody and mm.mlody into the workspace under root."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    shutil.copy2(_REAL_RULE_MLODY, root / "mlody" / "core" / "rule.mlody")
    shutil.copy2(_REAL_MM_MLODY, root / "mlody" / "common" / "mm.mlody")


# ---------------------------------------------------------------------------
# .mlody content templates
# ---------------------------------------------------------------------------

BUILTINS_MLODY = """\
def root(name, path, description=""):
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
"""

ROOTS_MLODY = """\
load("//mlody/core/builtins.mlody", "root")

root(name="data", path="//teams/data", description="parquet test root")
"""

# A value entity backed by a Parquet file.
# The path is a placeholder — we substitute the actual tmp_path in the test.
_PARQUET_VALUE_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="my_dataset",
    type=None,
    location=struct(kind="posix", type="parquet", name="dataset_loc", path="{parquet_path}"),
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""

_POSIX_PARQUET_VALUE_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="my_dataset",
    type=None,
    location=struct(kind="location", type="posix", name="dataset_loc", path="{parquet_path}"),
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""

# A value entity with a standard (non-Parquet) location for regression test.
_PLAIN_VALUE_MLODY = """\
builtins.register("value", struct(
    kind="value",
    name="plain_value",
    type=None,
    location=None,
    default=None,
    source=None,
    _lineage=[],
))
"""

_REMOTE_CSV_VALUE_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="remote_dataset",
    type=None,
    location=struct(
        kind="location",
        type="remote",
        name="remote_loc",
        uri="{uri}",
        attributes={{"uri": "{uri}"}},
    ),
    representation=struct(
        kind="representation",
        name="csv",
        separator=",",
        header_required=True,
        multifile=False,
        attributes={{
            "separator": ",",
            "header_required": True,
            "multifile": False,
        }},
    ),
    default=None,
    source=None,
    _lineage=[],
))
"""

_SOURCE_BACKED_LOCAL_CSV_VALUE_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="remote_dataset",
    type=None,
    location=struct(
        kind="location",
        type="remote",
        name="remote_loc",
        uri="{uri}",
        attributes={{"uri": "{uri}"}},
    ),
    representation=struct(
        kind="representation",
        name="csv",
        separator=",",
        header_required=True,
        multifile=False,
        attributes={{
            "separator": ",",
            "header_required": True,
            "multifile": False,
        }},
    ),
    default=None,
    source=None,
    _lineage=[],
))

builtins.register("value", struct(
    kind="value",
    name="local_dataset",
    type=None,
    location=struct(
        kind="location",
        type="posix",
        name="local_loc",
        path="{local_path}",
        attributes={{"path": ["{local_path}"]}},
    ),
    representation=struct(
        kind="representation",
        name="csv",
        separator=",",
        header_required=True,
        multifile=False,
        attributes={{
            "separator": ",",
            "header_required": True,
            "multifile": False,
        }},
    ),
    default=None,
    source=":remote_dataset",
    _source_value=builtins.lookup("value", "remote_dataset"),
    _lineage=[],
))
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parquet_file(path: Path) -> None:
    """Write a small Parquet file with columns id (int) and label (string)."""
    table = pa.table({
        "id": pa.array([0, 1, 2, 3, 4]),
        "label": pa.array(["cat", "dog", "bird", "fish", "hamster"]),
        "score": pa.array([0.1, 0.2, 0.3, 0.4, 0.5]),
    })
    pq.write_table(table, str(path))


def _make_workspace(root: Path, parquet_path: Path) -> Workspace:
    """Create a minimal workspace rooted at *root* with one Parquet-backed value entity."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    # types.mlody required for Workspace; write a minimal stub.
    (root / "mlody" / "common" / "types.mlody").write_text("")
    # mm.mlody and rule.mlody required by workspace_loader Phase 1.
    _add_mm_files(root)

    mlody_content = _PARQUET_VALUE_MLODY_TEMPLATE.format(
        parquet_path=str(parquet_path)
    )
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(mlody_content)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


def _make_posix_parquet_workspace(root: Path, parquet_path: Path) -> Workspace:
    """Create a workspace with a path-backed Parquet value entity."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")

    mlody_content = _POSIX_PARQUET_VALUE_MLODY_TEMPLATE.format(
        parquet_path=str(parquet_path)
    )
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(mlody_content)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


def _make_workspace_with_plain_value(root: Path) -> Workspace:
    """Create a minimal workspace with a non-Parquet value entity."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    # mm.mlody and rule.mlody required by workspace_loader Phase 1.
    _add_mm_files(root)
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(_PLAIN_VALUE_MLODY)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


def _make_remote_csv_workspace(root: Path, uri: str) -> Workspace:
    """Create a minimal workspace with one remote CSV-backed value entity."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(
        _REMOTE_CSV_VALUE_MLODY_TEMPLATE.format(uri=uri)
    )

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


def _make_source_backed_local_csv_workspace(
    root: Path,
    uri: str,
    local_path: Path,
) -> Workspace:
    """Create a workspace with a local CSV value backed by a remote CSV source."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(
        _SOURCE_BACKED_LOCAL_CSV_VALUE_MLODY_TEMPLATE.format(
            uri=uri,
            local_path=str(local_path),
        )
    )

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


# ---------------------------------------------------------------------------
# 5.1 Integration test: end-to-end index access (TEST-P-006)
# ---------------------------------------------------------------------------


class TestParquetIndexAccess:
    """Requirement: End-to-end Parquet label resolution via resolve_label_to_value."""

    def test_end_to_end_index_access_returns_raw_attr_value(
        self, tmp_path: Path
    ) -> None:
        """Scenario: End-to-end index access through resolve_label_to_value.

        WHEN a Parquet file with columns ["id", "label"] is on disk, a workspace
        is loaded with a value(...) entity pointing to it with location.type="parquet",
        and resolve_label_to_value is called with a label whose path is [0]
        THEN the result is _RawAttrValue wrapping the first-row dict.
        """
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        # The entity_query "[0]" will be parsed as IndexSegment(0) post-step,
        # but we use attribute traversal to drive the Parquet path directly.
        # The ParquetTraversalStrategy is invoked from ValueTraversalStrategy
        # when location.type == "parquet".
        label = parse_label("@data//pkg/dataset:my_dataset")
        # Access row 0 via entity_query bracket syntax
        label_with_idx = parse_label("@data//pkg/dataset:my_dataset[0]")
        result = resolve_label_to_value(label_with_idx, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        row = result.value
        assert isinstance(row, dict)
        assert row["id"] == 0
        assert row["label"] == "cat"

    def test_end_to_end_bare_entity_returns_value_value(
        self, tmp_path: Path
    ) -> None:
        """No path on a Parquet entity returns MlodyValueValue wrapping the struct."""
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        label = parse_label("@data//pkg/dataset:my_dataset")
        result = resolve_label_to_value(label, ws)

        # No traversal path → struct is wrapped as-is
        assert isinstance(result, MlodyValueValue)

    def test_end_to_end_sql_entity_query_returns_filtered_rows(
        self, tmp_path: Path
    ) -> None:
        """SQL entity-query suffix filters rows through the tabular helper."""
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        label = parse_label("@data//pkg/dataset:my_dataset[@sql WHERE score > 0.3]")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [3, 4]
        assert [row["label"] for row in rows] == ["fish", "hamster"]

    def test_sql_entity_query_on_path_backed_parquet_value_returns_filtered_rows(
        self, tmp_path: Path
    ) -> None:
        """Path-backed parquet values still support SQL suffix traversal."""
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_posix_parquet_workspace(tmp_path, parquet_file)

        label = parse_label("@data//pkg/dataset:my_dataset[@sql WHERE score > 0.3]")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [3, 4]
        assert [row["label"] for row in rows] == ["fish", "hamster"]

    def test_sql_entity_query_on_non_tabular_value_returns_unresolved(
        self, tmp_path: Path
    ) -> None:
        """Non-tabular SQL suffixes fail softly with a descriptive reason."""
        ws = _make_workspace_with_plain_value(tmp_path)

        label = parse_label("@data//pkg/dataset:plain_value[@sql WHERE score > 0.3]")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "tabular value" in result.reason
        assert "plain_value" in result.reason

    def test_remote_csv_sql_entity_query_returns_filtered_rows(
        self, tmp_path: Path
    ) -> None:
        """Remote CSV values execute SQL queries through staged tabular input."""
        csv_path = tmp_path / "employees.csv"
        csv_path.write_text("id,label,score\n0,cat,0.1\n1,dog,0.4\n2,bird,0.6\n")
        ws = _make_remote_csv_workspace(tmp_path, "https://example.com/employees.csv")

        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = SimpleNamespace(
                uri="https://example.com/employees.csv",
                path=csv_path,
                content_hash="abc123",
            )
            label = parse_label(
                "@data//pkg/dataset:remote_dataset[@sql WHERE score > 0.3]"
            )
            result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [1, 2]
        mock_stage.assert_called_once_with("https://example.com/employees.csv")

    def test_remote_csv_slice_entity_query_returns_rows(
        self, tmp_path: Path
    ) -> None:
        """Remote CSV values support non-SQL slice entity-query suffixes."""
        csv_path = tmp_path / "employees.csv"
        csv_path.write_text("id,label,score\n0,cat,0.1\n1,dog,0.4\n2,bird,0.6\n")
        ws = _make_remote_csv_workspace(tmp_path, "https://example.com/employees.csv")

        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = SimpleNamespace(
                uri="https://example.com/employees.csv",
                path=csv_path,
                content_hash="abc123",
            )
            label = parse_label("@data//pkg/dataset:remote_dataset[:2]")
            result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [0, 1]
        assert [row["label"] for row in rows] == ["cat", "dog"]
        mock_stage.assert_called_once_with("https://example.com/employees.csv")

    def test_source_backed_local_csv_sql_entity_query_materializes_cache(
        self, tmp_path: Path
    ) -> None:
        """Source-backed local CSV values materialize locally before SQL filtering."""
        csv_path = tmp_path / "employees.csv"
        csv_path.write_text("id,label,score\n0,cat,0.1\n1,dog,0.4\n2,bird,0.6\n")
        local_path = tmp_path / "artifacts" / "employees.csv"
        ws = _make_source_backed_local_csv_workspace(
            tmp_path,
            "https://example.com/employees.csv",
            local_path,
        )

        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = SimpleNamespace(
                uri="https://example.com/employees.csv",
                path=csv_path,
                content_hash="abc123",
            )
            label = parse_label(
                "@data//pkg/dataset:local_dataset[@sql WHERE score > 0.3]"
            )
            result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [1, 2]
        assert local_path.exists()
        assert local_path.read_text() == csv_path.read_text()
        mock_stage.assert_called_once_with("https://example.com/employees.csv")

    def test_source_backed_local_csv_slice_entity_query_returns_rows(
        self, tmp_path: Path
    ) -> None:
        """Slice entity-query suffixes work on source-backed local CSV values."""
        csv_path = tmp_path / "employees.csv"
        csv_path.write_text("id,label,score\n0,cat,0.1\n1,dog,0.4\n2,bird,0.6\n")
        local_path = tmp_path / "artifacts" / "employees.csv"
        ws = _make_source_backed_local_csv_workspace(
            tmp_path,
            "https://example.com/employees.csv",
            local_path,
        )

        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = SimpleNamespace(
                uri="https://example.com/employees.csv",
                path=csv_path,
                content_hash="abc123",
            )
            label = parse_label("@data//pkg/dataset:local_dataset[:2]")
            result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        rows = result.value
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [0, 1]
        assert [row["label"] for row in rows] == ["cat", "dog"]
        assert local_path.exists()
        assert local_path.read_text() == csv_path.read_text()
        mock_stage.assert_called_once_with("https://example.com/employees.csv")

    def test_workspace_resolve_sql_entity_query_returns_filtered_rows(
        self, tmp_path: Path
    ) -> None:
        """Workspace.resolve uses the resolver hook for SQL entity-query suffixes."""
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        result = ws.resolve("@data//pkg/dataset:my_dataset[@sql WHERE score > 0.3]")

        assert isinstance(result, list)
        assert [row["id"] for row in result] == [3, 4]


# ---------------------------------------------------------------------------
# 5.2 Integration test: index + field chained access (TEST-P-006)
# ---------------------------------------------------------------------------


class TestParquetChainedAccess:
    """Requirement: Chained index + field access through resolve_label_to_value."""

    def test_index_plus_field_via_attribute_path(
        self, tmp_path: Path
    ) -> None:
        """Scenario: End-to-end field access through resolve_label_to_value.

        WHEN the path is [IndexSegment(0), FieldSegment("label")]
        THEN the result is MlodyValueValue promoted from the string "cat"
        (scalar promotion: single-row extraction §6.2).

        We drive this via ParquetTraversalStrategy directly (bypassing the label
        parser's string-only attr_path) to confirm the strategy handles chaining.
        """
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        # Fetch the struct from the registry to drive strategy directly
        from mlody.resolver.label_value import (
            ParquetTraversalStrategy,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import FieldSegment, IndexSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/dataset", "my_dataset")
        assert lookup is not None, "Entity not found in registry"
        _, struct = lookup

        label = parse_label("@data//pkg/dataset:my_dataset")
        strategy = ParquetTraversalStrategy()

        # [0].label → scalar promotion: MlodyValueValue with data=("cat",)
        result = strategy.traverse(
            struct,
            (IndexSegment(0), FieldSegment("label")),
            label,
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.location.data == ("cat",)  # type: ignore[union-attr]
        assert result.struct.type.attributes["element_type"].name == "string"  # type: ignore[union-attr]

    def test_source_backed_local_csv_slice_plus_field_via_value_strategy(
        self, tmp_path: Path
    ) -> None:
        """Non-parquet tabular values support slice-plus-field through ValueTraversalStrategy."""
        csv_path = tmp_path / "employees.csv"
        csv_path.write_text("id,label,score\n0,cat,0.1\n1,dog,0.4\n2,bird,0.6\n")
        local_path = tmp_path / "artifacts" / "employees.csv"
        ws = _make_source_backed_local_csv_workspace(
            tmp_path,
            "https://example.com/employees.csv",
            local_path,
        )

        from mlody.resolver.label_value import ValueTraversalStrategy, _lookup_entity
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/dataset", "local_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/dataset:local_dataset")
        strategy = ValueTraversalStrategy()

        with patch("mlody.core.tabular.remote_staging.stage_remote_file") as mock_stage:
            mock_stage.return_value = SimpleNamespace(
                uri="https://example.com/employees.csv",
                path=csv_path,
                content_hash="abc123",
            )
            result = strategy.traverse(
                struct,
                (SliceSegment(0, 2, None), FieldSegment("label")),
                label,
            )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.location.data == ("cat", "dog")  # type: ignore[union-attr]
        assert result.struct.type.attributes["element_type"].name == "string"  # type: ignore[union-attr]
        assert local_path.exists()
        assert local_path.read_text() == csv_path.read_text()
        mock_stage.assert_called_once_with("https://example.com/employees.csv")

    def test_slice_plus_field_via_strategy(
        self, tmp_path: Path
    ) -> None:
        """Slice then field extracts one column from multiple rows, promoting to MlodyValueValue.

        WHEN the path is [SliceSegment(0, 3, None), FieldSegment("label")]
        THEN the result is MlodyValueValue promoted from ["cat", "dog", "bird"]
        (mapped traversal scalar promotion §6.1).
        """
        parquet_file = tmp_path / "train.parquet"
        _make_parquet_file(parquet_file)
        ws = _make_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import (
            ParquetTraversalStrategy,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/dataset", "my_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/dataset:my_dataset")
        strategy = ParquetTraversalStrategy()

        # [0:3].label → MlodyValueValue with data=("cat", "dog", "bird")
        result = strategy.traverse(
            struct,
            (SliceSegment(0, 3, None), FieldSegment("label")),
            label,
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.location.data == ("cat", "dog", "bird")  # type: ignore[union-attr]
        assert result.struct.type.attributes["element_type"].name == "string"  # type: ignore[union-attr]

    def test_missing_location_returns_unresolved(
        self, tmp_path: Path
    ) -> None:
        """Scenario: Missing location path returns MlodyUnresolvedValue."""
        from mlody.resolver.label_value import ParquetTraversalStrategy
        from mlody.core.traversal_grammar import IndexSegment
        from common.python.starlarkish.core.struct import Struct

        label = parse_label("@data//pkg/dataset:my_dataset")
        struct_no_path = Struct(
            kind="value",
            name="no_path",
            location=Struct(kind="posix", type="parquet", name="loc"),
            # no 'path' attribute on the location
        )

        strategy = ParquetTraversalStrategy()
        result = strategy.traverse(
            struct_no_path,
            (IndexSegment(0),),
            label,
        )

        assert isinstance(result, MlodyUnresolvedValue)
        assert "path" in result.reason.lower()

    def test_file_not_found_returns_unresolved(
        self, tmp_path: Path
    ) -> None:
        """Scenario: File not found returns MlodyUnresolvedValue."""
        from mlody.resolver.label_value import ParquetTraversalStrategy
        from mlody.core.traversal_grammar import IndexSegment
        from common.python.starlarkish.core.struct import Struct

        label = parse_label("@data//pkg/dataset:my_dataset")
        struct_bad_path = Struct(
            kind="value",
            name="bad",
            location=Struct(
                kind="posix",
                type="parquet",
                name="loc",
                path=str(tmp_path / "nonexistent.parquet"),
            ),
        )

        strategy = ParquetTraversalStrategy()
        result = strategy.traverse(
            struct_bad_path,
            (IndexSegment(0),),
            label,
        )

        assert isinstance(result, MlodyUnresolvedValue)
        assert "nonexistent" in result.reason


# ---------------------------------------------------------------------------
# 5.3 Regression test: non-Parquet kind="value" label still resolves (TEST-P-007)
# ---------------------------------------------------------------------------


class TestNonParquetRegression:
    """Requirement: Non-Parquet value entities resolve as before (TEST-P-007)."""

    def test_plain_value_entity_still_resolves(
        self, tmp_path: Path
    ) -> None:
        """Scenario: Existing non-Parquet kind="value" label still resolves correctly.

        WHEN a label resolves to a kind="value" Starlark Struct with a
        non-parquet location and resolve_label_to_value is called
        THEN the result is MlodyValueValue wrapping the struct (unchanged behavior).
        """
        ws = _make_workspace_with_plain_value(tmp_path)

        label = parse_label("@data//pkg/dataset:plain_value")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "name", None) == "plain_value"

    def test_plain_value_attribute_traversal_still_works(
        self, tmp_path: Path
    ) -> None:
        """Attribute traversal on a non-Parquet value remains unchanged."""
        ws = _make_workspace_with_plain_value(tmp_path)

        label = parse_label("@data//pkg/dataset:plain_value.name")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == "plain_value"


# ---------------------------------------------------------------------------
# TestScalarPromotionParquet — FR-001, FR-002, FR-003, spec §6.1–§6.2
# ---------------------------------------------------------------------------

# Parquet file with a richer schema for promotion tests.
def _make_rich_parquet_file(path: Path) -> None:
    """Write a Parquet file with bool, int64, string, and timestamp columns."""
    import pyarrow.lib as _pa_lib  # noqa: PLC0415

    table = pa.table({
        "Bald": pa.array([True, False, True, False, True], type=pa.bool_()),
        "id": pa.array([0, 1, 2, 3, 4], type=pa.int64()),
        "label": pa.array(["cat", "dog", "bird", "fish", "hamster"], type=pa.string()),
        "ts": pa.array([0, 1, 2, 3, 4], type=pa.timestamp("s")),
        # struct-typed column (nested) — should NOT be promoted
        "meta": pa.StructArray.from_arrays(
            [pa.array([10, 20, 30, 40, 50], type=pa.int64())],
            names=["size"],
        ),
    })
    pq.write_table(table, str(path))


_RICH_PARQUET_VALUE_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="rich_dataset",
    type=None,
    location=struct(kind="posix", type="parquet", name="loc", path="{parquet_path}"),
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""

# Value entity with a declared mlody type that specifies field types.
# The 'id' field is declared as bool() but the Arrow schema has int64 → mismatch.
_MISMATCH_MLODY_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="mismatch_dataset",
    type=struct(
        kind="type",
        type="record",
        name="record",
        _root_kind="record",
        fields=[
            struct(
                kind="field",
                name="id",
                type=struct(kind="type", type="bool", name="bool", _root_kind="bool", attributes={{}}, _allowed_attrs={{}}),
            ),
        ],
        attributes={{}},
        _allowed_attrs={{}},
    ),
    location=struct(kind="posix", type="parquet", name="loc", path="{parquet_path}"),
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""


def _make_rich_workspace(root: Path, parquet_path: Path) -> Workspace:
    """Workspace with a rich-schema Parquet entity at teams/data/pkg/rich."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    # mm.mlody and rule.mlody required by workspace_loader Phase 1.
    _add_mm_files(root)

    content = _RICH_PARQUET_VALUE_MLODY_TEMPLATE.format(parquet_path=str(parquet_path))
    (root / "teams" / "data" / "pkg" / "rich.mlody").write_text(content)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


def _make_mismatch_workspace(root: Path, parquet_path: Path) -> Workspace:
    """Workspace with a Parquet entity whose mlody type disagrees with the schema."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    # mm.mlody and rule.mlody required by workspace_loader Phase 1.
    _add_mm_files(root)

    content = _MISMATCH_MLODY_TEMPLATE.format(parquet_path=str(parquet_path))
    (root / "teams" / "data" / "pkg" / "mismatch.mlody").write_text(content)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


class TestScalarPromotionParquet:
    """Requirement: scalar promotion from Parquet traversal (FR-001, FR-002, FR-003, §6.1–§6.2)."""

    def test_slice_field_bool_promotes_to_value_value(self, tmp_path: Path) -> None:
        """FR-001/FR-002: Slicing then FieldSegment on bool column → MlodyValueValue.

        WHEN the path is [0:3].Bald on a parquet with Bald: bool
        THEN result is MlodyValueValue with element_type.name == 'bool'
        and data contains the three extracted booleans.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import ParquetTraversalStrategy, _lookup_entity
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("Bald")), label
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.type.attributes["element_type"].name == "bool"  # type: ignore[union-attr]
        assert result.struct.location.data == (True, False, True)  # type: ignore[union-attr]

    def test_slice_field_int_promotes_to_value_value(self, tmp_path: Path) -> None:
        """FR-001/FR-002: Slicing then FieldSegment on int64 column → MlodyValueValue.

        WHEN the path is [0:3].id on a parquet with id: int64
        THEN result is MlodyValueValue with element_type.name == 'integer'.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import ParquetTraversalStrategy, _lookup_entity
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("id")), label
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.type.attributes["element_type"].name == "integer"  # type: ignore[union-attr]

    def test_slice_field_string_promotes_to_value_value(self, tmp_path: Path) -> None:
        """FR-001/FR-002: Slicing then FieldSegment on string column → MlodyValueValue.

        WHEN the path is [0:3].label on a parquet with label: string
        THEN result is MlodyValueValue with element_type.name == 'string'.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import ParquetTraversalStrategy, _lookup_entity
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("label")), label
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert result.struct.type.attributes["element_type"].name == "string"  # type: ignore[union-attr]
        assert result.struct.location.data == ("cat", "dog", "bird")  # type: ignore[union-attr]

    def test_index_field_produces_one_element_vector(self, tmp_path: Path) -> None:
        """FR-001/FR-002: IndexSegment then FieldSegment → one-element MlodyValueValue.

        WHEN the path is [0].label
        THEN result is MlodyValueValue with len(data) == 1.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import ParquetTraversalStrategy, _lookup_entity
        from mlody.core.traversal_grammar import FieldSegment, IndexSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (IndexSegment(0), FieldSegment("label")), label
        )

        assert isinstance(result, MlodyValueValue), f"Expected MlodyValueValue, got {result!r}"
        assert len(result.struct.location.data) == 1  # type: ignore[union-attr]
        assert result.struct.location.data == ("cat",)  # type: ignore[union-attr]

    def test_unsupported_arrow_type_returns_unresolved(self, tmp_path: Path) -> None:
        """FR-002: Arrow timestamp column → MlodyUnresolvedValue with field name and type.

        WHEN the path is [0:3].ts on a parquet where ts is timestamp
        THEN result is MlodyUnresolvedValue containing the field name and Arrow type.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import (
            MlodyUnresolvedValue,
            ParquetTraversalStrategy,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("ts")), label
        )

        assert isinstance(result, MlodyUnresolvedValue), f"Expected MlodyUnresolvedValue, got {result!r}"
        assert "ts" in result.reason
        assert "timestamp" in result.reason.lower() or "no mlody primitive" in result.reason

    def test_nested_arrow_column_not_promoted(self, tmp_path: Path) -> None:
        """FR-006/FR-010: Arrow struct-typed column is returned as _RawAttrValue unchanged.

        WHEN the path is [0:3].meta on a parquet where meta is struct-typed
        THEN result is _RawAttrValue (no promotion for nested types).
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import (
            ParquetTraversalStrategy,
            _RawAttrValue,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("meta")), label
        )

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue for struct column, got {result!r}"

    def test_declaration_mismatch_returns_unresolved(self, tmp_path: Path) -> None:
        """FR-003: mlody declaration conflicts with Arrow schema → MlodyUnresolvedValue.

        WHEN the Arrow schema has id: int64 but mlody declares id as bool()
        THEN result is MlodyUnresolvedValue naming both type names.
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_mismatch_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import (
            MlodyUnresolvedValue,
            ParquetTraversalStrategy,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import FieldSegment, SliceSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/mismatch", "mismatch_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/mismatch:mismatch_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (SliceSegment(0, 3, None), FieldSegment("id")), label
        )

        assert isinstance(result, MlodyUnresolvedValue), f"Expected MlodyUnresolvedValue, got {result!r}"
        assert "mismatch" in result.reason.lower()
        # Should name both the inferred type ('integer') and declared type ('bool')
        assert "integer" in result.reason
        assert "bool" in result.reason

    def test_backward_compat_raw_attr_value_still_works(self, tmp_path: Path) -> None:
        """NFR-002: Raw index access (no field step) still returns _RawAttrValue.

        WHEN the path is just [0] (no field segment)
        THEN result is _RawAttrValue wrapping the row dict (unchanged from before).
        """
        parquet_file = tmp_path / "rich.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_rich_workspace(tmp_path, parquet_file)

        from mlody.resolver.label_value import (
            ParquetTraversalStrategy,
            _RawAttrValue,
            _lookup_entity,
        )
        from mlody.core.traversal_grammar import IndexSegment

        lookup = _lookup_entity(ws, "teams/data/pkg/rich", "rich_dataset")
        assert lookup is not None
        _, struct = lookup

        label = parse_label("@data//pkg/rich:rich_dataset")
        result = ParquetTraversalStrategy().traverse(
            struct, (IndexSegment(0),), label
        )

        # Raw row dict — no promotion attempted when no field segment
        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue for row dict, got {result!r}"
        assert isinstance(result.value, dict)
        assert result.value["label"] == "cat"


# ---------------------------------------------------------------------------
# Regression: end-to-end traversal of `<entity>.<field>[1:4].<col>` where the
# field is parquet-backed.  This shape (record-typed root, parquet field, slice,
# column) repeatedly leaked the str/PathSegment asymmetry across strategy
# boundaries — see plan "Fix `.valid[1:4].Bald` traversal".
# ---------------------------------------------------------------------------

# Record-typed root with one field `valid` whose declaration carries a
# parquet representation and location.  After `:dataset.valid`, the rebuilt
# accumulator is parquet-backed and the engine hands the remaining segments
# off to ParquetTraversalStrategy.
_RECORD_WITH_PARQUET_FIELD_TEMPLATE = """\
builtins.register("value", struct(
    kind="value",
    name="dataset",
    type=struct(
        kind="type",
        type="record",
        name="record",
        _root_kind="record",
        attributes={{}},
        _allowed_attrs={{}},
        fields=[
            struct(
                kind="field",
                name="valid",
                type=struct(
                    kind="type",
                    type="vector",
                    name="vector",
                    _root_kind="vector",
                    attributes={{}},
                    _allowed_attrs={{}},
                ),
                representation=struct(
                    kind="representation",
                    type="parquet",
                    name="parquet",
                ),
                location=struct(
                    kind="posix",
                    type="parquet",
                    name="loc",
                    path="{parquet_path}",
                ),
            ),
        ],
    ),
    location=None,
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""


def _make_record_with_parquet_field_workspace(
    root: Path, parquet_path: Path
) -> Workspace:
    """Workspace with `:dataset` whose `valid` field is parquet-backed."""
    (root / "mlody" / "core").mkdir(parents=True, exist_ok=True)
    (root / "mlody" / "common").mkdir(parents=True, exist_ok=True)
    (root / "teams" / "data" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "mlody" / "core" / "builtins.mlody").write_text(BUILTINS_MLODY)
    (root / "mlody" / "roots.mlody").write_text(ROOTS_MLODY)
    (root / "mlody" / "common" / "types.mlody").write_text("")
    # mm.mlody and rule.mlody required by workspace_loader Phase 1.
    _add_mm_files(root)

    content = _RECORD_WITH_PARQUET_FIELD_TEMPLATE.format(parquet_path=str(parquet_path))
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(content)

    ws = Workspace(monorepo_root=root, skipped_mlody_paths=[])
    ws.load()
    return ws


class TestParquetFieldSliceFieldRegression:
    """Regression: `:dataset.valid[1:4].Bald` end-to-end with a parquet field."""

    def test_record_field_slice_field_promotes_to_vector_bool(
        self, tmp_path: Path
    ) -> None:
        """Resolving ``:dataset.valid[1:4].Bald`` on a record whose ``valid`` field
        is parquet-backed must return a MlodyValueValue with vector(bool) type
        and a 3-element inline tuple.

        This exercises the full pipeline:
          1. parser accepts inline ``[1:4]`` brackets in the attribute path
          2. resolver normalises the heterogeneous (str | PathSegment) tuple
             into ``tuple[PathSegment, ...]`` at the chokepoint
          3. ValueTraversalStrategy._traverse_with_engine handles the first
             FieldSegment("valid") and detects the parquet-backed accumulator
          4. handoff to ParquetTraversalStrategy with (SliceSegment, FieldSegment("Bald"))
          5. parquet strategy reads rows 1:4, extracts column Bald, promotes
             to MlodyValueValue with vector<bool> type
        """
        parquet_file = tmp_path / "valid.parquet"
        _make_rich_parquet_file(parquet_file)
        ws = _make_record_with_parquet_field_workspace(tmp_path, parquet_file)

        label = parse_label("@data//pkg/dataset:dataset.valid[1:4].Bald")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue), (
            f"Expected MlodyValueValue, got {result!r}"
        )
        # type=vector(element_type=bool())
        assert getattr(result.struct.type, "name", None) == "vector"  # type: ignore[union-attr]
        element_type = result.struct.type.attributes["element_type"]  # type: ignore[union-attr]
        assert getattr(element_type, "name", None) == "bool"
        # location is inline with a 3-tuple of bools (rows 1, 2, 3 of the fixture)
        assert getattr(result.struct.location, "type", None) == "inline"  # type: ignore[union-attr]
        assert result.struct.location.data == (False, True, False)  # type: ignore[union-attr]
