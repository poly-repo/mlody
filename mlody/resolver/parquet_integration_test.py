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

from pathlib import Path

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

    mlody_content = _PARQUET_VALUE_MLODY_TEMPLATE.format(
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
    (root / "teams" / "data" / "pkg" / "dataset.mlody").write_text(_PLAIN_VALUE_MLODY)

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
        THEN the result is _RawAttrValue wrapping the string "cat".

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

        # [0].label
        result = strategy.traverse(
            struct,
            (IndexSegment(0), FieldSegment("label")),
            label,
        )

        assert isinstance(result, _RawAttrValue), f"Expected _RawAttrValue, got {result!r}"
        assert result.value == "cat"

    def test_slice_plus_field_via_strategy(
        self, tmp_path: Path
    ) -> None:
        """Slice then field extracts one column from multiple rows."""
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

        # [0:3].label → ["cat", "dog", "bird"]
        result = strategy.traverse(
            struct,
            (SliceSegment(0, 3, None), FieldSegment("label")),
            label,
        )

        assert isinstance(result, _RawAttrValue)
        assert result.value == ["cat", "dog", "bird"]

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
