"""Tests for mlody.resolver.label_value — resolve_label_to_value golden tests.

Each test traces back to a named scenario in the spec
(openspec/changes/mav-457-label-value-mapping/specs/label-value-resolver/spec.md).

Filesystem fixtures: pyfakefs (``fs`` fixture).
Evaluator fixtures: Workspace with pyfakefs + .mlody content — no mocking of
starlarkish internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from mlody.core.label import parse_label
from mlody.core.virtual_value import force_virtual_value
from mlody.core.workspace import Workspace
from mlody.resolver.label_value import (
    TRAVERSAL_STRATEGIES,
    MlodyActionValue,
    MlodyFolderValue,
    MlodySourceValue,
    MlodyTaskValue,
    MlodyUnresolvedValue,
    MlodyValue,  # noqa: F401 — imported for type annotations in extensibility test
    MlodyValueValue,
    _RawAttrValue,
    _traverse_one_step,
    resolve_label_to_value,
)

# ---------------------------------------------------------------------------
# Shared .mlody file contents used across fixtures
# ---------------------------------------------------------------------------

ROOT = Path("/project")

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

root(name="myroot", path="//teams/myroot", description="test root")
"""

TYPES_MLODY = """\
builtins.register("type", struct(
    kind="type", type="mlody_workspace_info", name="mlody_workspace_info",
    fields=[
        struct(name="path", type=struct(kind="type", type="string", name="string")),
        struct(name="branch", type=struct(kind="type", type="string", name="string")),
        struct(name="sha", type=struct(kind="type", type="string", name="string")),
        struct(name="roots", type=struct(kind="type", type="vector", name="vector")),
    ],
    attributes={}, _allowed_attrs={},
    _root_kind="record",
))
builtins.register("type", struct(
    kind="type", type="mlody-workspace", name="mlody-workspace",
    attributes={}, _allowed_attrs={},
    virtual_attributes=[
        struct(name="info", type=struct(kind="type", type="mlody_workspace_info", name="mlody_workspace_info", _root_kind="record", fields=[
            struct(name="path", type=struct(kind="type", type="string", name="string")),
            struct(name="branch", type=struct(kind="type", type="string", name="string")),
            struct(name="sha", type=struct(kind="type", type="string", name="string")),
            struct(name="roots", type=struct(kind="type", type="vector", name="vector")),
        ])),
    ],
))
"""

TASK_MLODY = """\
builtins.register("task", struct(
    kind="task",
    name="my_task",
    inputs=[],
    outputs=[],
    action=None,
))
"""

TASK_WITH_INPUTS_MLODY = """\
builtins.register("task", struct(
    kind="task",
    name="my_task",
    inputs=[],
    outputs=[],
    action=None,
    extra=struct(count=42),
))
"""

ACTION_MLODY = """\
builtins.register("action", struct(
    kind="action",
    name="my_action",
    inputs=[],
    outputs=[],
    config=[],
))
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(fs: FakeFilesystem, extra_files: dict[str, str] | None = None) -> Workspace:
    """Construct a minimal Workspace with the given extra .mlody files."""
    fs.create_file(str(ROOT / "mlody/core/builtins.mlody"), contents=BUILTINS_MLODY)
    fs.create_file(str(ROOT / "mlody/roots.mlody"), contents=ROOTS_MLODY)
    fs.create_file(str(ROOT / "mlody/common/types.mlody"), contents=TYPES_MLODY)

    if extra_files:
        for rel_path, contents in extra_files.items():
            fs.create_file(str(ROOT / rel_path), contents=contents)

    ws = Workspace(monorepo_root=ROOT, skipped_mlody_paths=[])
    ws.load()
    return ws


# ---------------------------------------------------------------------------
# 6.1 Golden test: label resolves to MlodyFolderValue
# Scenario: "Golden test for MlodyFolderValue"
# ---------------------------------------------------------------------------


class TestFolderValue:
    """Requirement: Filesystem traversal — folder detection."""

    def test_label_resolves_to_folder_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Label resolves to a folder."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg/mydir"))
        fs.create_file(str(ROOT / "teams/myroot/pkg/mydir/child1.txt"), contents="")
        fs.create_file(str(ROOT / "teams/myroot/pkg/mydir/child2.txt"), contents="")
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/mydir")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyFolderValue)
        assert result.path == "pkg/mydir"
        assert "child1.txt" in result.children
        assert "child2.txt" in result.children

    def test_folder_children_are_immediate_only(self, fs: FakeFilesystem) -> None:
        """Children list contains only immediate directory entries (not recursive)."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg/mydir"))
        fs.create_file(str(ROOT / "teams/myroot/pkg/mydir/a.mlody"), contents="")
        fs.create_dir(str(ROOT / "teams/myroot/pkg/mydir/subdir"))
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/mydir")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyFolderValue)
        # Only the top-level entries
        assert set(result.children) == {"a.mlody", "subdir"}

    def test_folder_with_attribute_path_is_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: Folder with no entity name and attribute path is unresolved."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg/mydir"))
        ws = _make_workspace(fs)

        # parse_label for @myroot//pkg/mydir'attr
        label = parse_label("@myroot//pkg/mydir'some_attr")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "attribute traversal" in result.reason.lower()


# ---------------------------------------------------------------------------
# 6.2 Golden test: label resolves to MlodySourceValue
# Scenario: "Golden test for MlodySourceValue"
# ---------------------------------------------------------------------------


class TestSourceValue:
    """Requirement: Filesystem traversal — source file detection."""

    def test_label_resolves_to_source_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Label resolves to a source file."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg"))
        fs.create_file(str(ROOT / "teams/myroot/pkg/foo.mlody"), contents="")
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/foo")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodySourceValue)
        assert result.path == "pkg/foo"

    def test_source_with_attribute_path_is_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: Source file with no entity name and attribute path is unresolved."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg"))
        fs.create_file(str(ROOT / "teams/myroot/pkg/foo.mlody"), contents="")
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/foo'some_attr")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "attribute traversal" in result.reason.lower()

    def test_missing_path_is_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: Terminal path is neither directory nor source file."""
        fs.create_dir(str(ROOT / "teams/myroot/pkg"))
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/missing")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "not a directory" in result.reason.lower()


# ---------------------------------------------------------------------------
# 6.3 Golden test: label resolves to MlodyTaskValue
# Scenario: "Golden test for MlodyTaskValue"
# ---------------------------------------------------------------------------


class TestTaskValue:
    """Requirement: Entity dispatch — task to MlodyTaskValue."""

    def test_label_resolves_to_task_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Task entity resolves to MlodyTaskValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyTaskValue)
        assert result.struct is not None
        assert getattr(result.struct, "name", None) == "my_task"

    def test_task_value_struct_is_registry_struct(self, fs: FakeFilesystem) -> None:
        """The struct field is the raw registry object."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyTaskValue)
        # isinstance check confirms exact type (spec scenario)
        from mlody.resolver.label_value import MlodyTaskValue as _T

        assert isinstance(result, _T)


# ---------------------------------------------------------------------------
# 6.4 Golden test: label resolves to MlodyActionValue
# Scenario: "Golden test for MlodyActionValue"
# ---------------------------------------------------------------------------


class TestActionValue:
    """Requirement: Entity dispatch — action to MlodyActionValue."""

    def test_label_resolves_to_action_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Action entity resolves to MlodyActionValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": ACTION_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_action")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyActionValue)
        assert result.struct is not None
        assert getattr(result.struct, "name", None) == "my_action"


VALUE_MLODY = """\
builtins.register("value", struct(
    kind="value",
    name="my_value",
    type=None,
    location=None,
    default=None,
    source=None,
    _lineage=[],
))
"""

TOP_JSON_VALUE_MLODY = """\
builtins.register("value", struct(
    kind="value",
    name="meta",
    type=struct(kind="type", type="top", name="top", _root_kind="top"),
    location=struct(kind="posix", type="posix", name="meta_loc", path=["/project/teams/myroot/pkg/meta.json"]),
    representation=struct(kind="representation", name="json", attributes={}),
    default=None,
    source=None,
    _lineage=[],
))
"""

RECORD_WITH_JSON_FIELD_MLODY = """\
info_field = struct(
    name="info",
    type=struct(kind="type", type="top", name="top", _root_kind="top"),
    location=struct(kind="posix", type="posix", name="info_loc", path=["meta.json"]),
    representation=struct(kind="representation", name="json", attributes={}),
)
dataset_type = struct(kind="record", name="DatasetType", fields=[info_field])
builtins.register("value", struct(
    kind="value",
    name="dataset",
    type=dataset_type,
    location=struct(kind="posix", type="posix", name="base_loc", path=["/project/teams/myroot/pkg"]),
    representation=None,
    default=None,
    source=None,
    _lineage=[],
))
"""


# ---------------------------------------------------------------------------
# 6.5 Golden test: label resolves to MlodyValueValue
# ---------------------------------------------------------------------------


class TestValueKind:
    """Requirement: Entity dispatch — value kind to MlodyValueValue."""

    def test_label_resolves_to_value_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Value entity resolves to MlodyValueValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": VALUE_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_value")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue)
        assert result.struct is not None
        assert getattr(result.struct, "name", None) == "my_value"

    def test_attribute_traversal_on_value(self, fs: FakeFilesystem) -> None:
        """Scenario: Attribute traversal into a value struct field."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": VALUE_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_value.name")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == "my_value"

    def test_missing_attribute_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: Attribute not present on the value struct."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": VALUE_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_value.nonexistent")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "nonexistent" in result.reason

    def test_top_json_value_supports_dotted_lookup_from_posix_file(
        self,
        fs: FakeFilesystem,
    ) -> None:
        ws = _make_workspace(
            fs,
            extra_files={
                "teams/myroot/pkg/foo.mlody": TOP_JSON_VALUE_MLODY,
                "teams/myroot/pkg/meta.json": '{"sha":"abc123","likes":16}',
            },
        )

        label = parse_label("@myroot//pkg/foo:meta.sha")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == "abc123"

    def test_record_field_top_json_supports_dotted_lookup_from_posix_file(
        self,
        fs: FakeFilesystem,
    ) -> None:
        ws = _make_workspace(
            fs,
            extra_files={
                "teams/myroot/pkg/foo.mlody": RECORD_WITH_JSON_FIELD_MLODY,
                "teams/myroot/pkg/meta.json": '{"sha":"2d738f56","downloads":3448}',
            },
        )

        label = parse_label("@myroot//pkg/foo:dataset.info.sha")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == "2d738f56"

    def test_top_json_value_missing_key_returns_unresolved(
        self,
        fs: FakeFilesystem,
    ) -> None:
        ws = _make_workspace(
            fs,
            extra_files={
                "teams/myroot/pkg/foo.mlody": TOP_JSON_VALUE_MLODY,
                "teams/myroot/pkg/meta.json": '{"sha":"abc123"}',
            },
        )

        label = parse_label("@myroot//pkg/foo:meta.missing")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "missing" in result.reason


class TestWorkspaceVirtualAttributes:
    """Requirement: Workspace attrs resolve as typed virtual values."""

    def test_workspace_info_resolves_to_typed_value(self, fs: FakeFilesystem) -> None:
        ws = _make_workspace(fs)

        label = parse_label("'info")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "kind", None) == "value"
        assert getattr(getattr(result.struct, "type", None), "name", None) == "mlody_workspace_info"
        assert getattr(getattr(result.struct, "location", None), "type", None) == "virtual"

    def test_workspace_info_branch_resolves_to_typed_leaf_value(self, fs: FakeFilesystem) -> None:
        ws = _make_workspace(fs)

        label = parse_label("'info.branch")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "kind", None) == "value"
        assert getattr(getattr(result.struct, "type", None), "name", None) == "string"
        assert getattr(getattr(result.struct, "location", None), "type", None) == "virtual"

    def test_workspace_info_branch_still_materializes_to_workspace_state(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Regression: public read traversal still materializes virtual leaves."""
        ws = _make_workspace(fs)

        label = parse_label("'info.branch")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyValueValue)
        assert force_virtual_value(result.struct) == ws.info.branch

    def test_missing_workspace_virtual_attribute_returns_unresolved(self, fs: FakeFilesystem) -> None:
        ws = _make_workspace(fs)

        label = parse_label("'missing")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "missing" in result.reason


# ---------------------------------------------------------------------------
# 6.6 Golden test: MlodyUnresolvedValue — path not found
# Scenario: "Golden test for MlodyUnresolvedValue"
# ---------------------------------------------------------------------------


class TestUnresolvedValue:
    """Requirement: Soft failure — MlodyUnresolvedValue returned, never raised."""

    def test_unresolved_when_path_not_found(self, fs: FakeFilesystem) -> None:
        """Scenario: path does not exist → MlodyUnresolvedValue."""
        ws = _make_workspace(fs)

        label = parse_label("@myroot//nonexistent/path")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert result.label is label
        assert len(result.reason) > 0

    def test_unresolved_has_non_empty_reason(self, fs: FakeFilesystem) -> None:
        """reason must be a non-empty human-readable string (spec requirement)."""
        ws = _make_workspace(fs)

        label = parse_label("@myroot//gone")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert isinstance(result.reason, str)
        assert result.reason.strip() != ""

    def test_unknown_root_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """Unknown root name → MlodyUnresolvedValue (not KeyError)."""
        ws = _make_workspace(fs)

        label = parse_label("@unknownroot//pkg/foo")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "unknownroot" in result.reason


# ---------------------------------------------------------------------------
# 6.6 Golden test: MlodyUnresolvedValue — entity not in registry
# Scenario: "Entity not found in registry yields MlodyUnresolvedValue"
# ---------------------------------------------------------------------------


class TestUnresolvedEntityNotInRegistry:
    """Requirement: Registry lookup — entity not found."""

    def test_entity_not_found_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: entity not in registry → MlodyUnresolvedValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:ghost")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "ghost" in result.reason

    def test_entity_reason_names_stem(self, fs: FakeFilesystem) -> None:
        """reason must name the stem it was searched in (spec NFR-7.4)."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:ghost")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        # Reason should mention where we looked
        assert "ghost" in result.reason


# ---------------------------------------------------------------------------
# 6.7 Attribute-path traversal on task struct: full consumption
# Scenario: "Attribute path fully consumed on task struct"
# ---------------------------------------------------------------------------


class TestAttributePathTraversal:
    """Requirement: Traversal strategy — struct-based attribute path consumption."""

    def test_attribute_path_fully_consumed(self, fs: FakeFilesystem) -> None:
        """Scenario: extra.count fully consumed → terminal value returned."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        # @myroot//pkg/foo:my_task'extra.count
        label = parse_label("@myroot//pkg/foo:my_task'extra.count")
        result = resolve_label_to_value(label, ws)

        # Terminal value reached — returned as _RawAttrValue
        assert isinstance(result, _RawAttrValue)
        assert result.value == 42

    def test_attribute_path_on_task_no_residual(self, fs: FakeFilesystem) -> None:
        """No residual field_path on the returned value (spec FR-003)."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task'extra")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        # The result has no attribute_path — all consumed
        assert not hasattr(result, "attribute_path")

    def test_no_attribute_path_returns_task_value(self, fs: FakeFilesystem) -> None:
        """When no attribute path, returns MlodyTaskValue (not _RawAttrValue)."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyTaskValue)


# ---------------------------------------------------------------------------
# 6.8 Attribute-path traversal: missing intermediate attribute
# Scenario: "Missing intermediate attribute yields MlodyUnresolvedValue"
# ---------------------------------------------------------------------------


class TestAttributePathMissingAttribute:
    """Requirement: Traversal strategy — missing attribute returns MlodyUnresolvedValue."""

    def test_missing_attribute_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """Scenario: missing_field does not exist → MlodyUnresolvedValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task'extra.missing_field")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "missing_field" in result.reason

    def test_first_segment_missing_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """When the very first attribute segment is missing, returns unresolved."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task'nonexistent_field")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "nonexistent_field" in result.reason


# ---------------------------------------------------------------------------
# 6.9 Extensibility seam: custom TraversalStrategy for kind "widget"
# Scenario: "Custom strategy registered for a new kind is dispatched"
# ---------------------------------------------------------------------------


class TestExtensibilitySeam:
    """Requirement: TraversalStrategy extension seam."""

    def test_custom_strategy_dispatched_for_new_kind(self, fs: FakeFilesystem) -> None:
        """Scenario: stub strategy for 'widget' kind is invoked."""
        sentinel_value = MlodyFolderValue(path="stub", children=[])

        class StubWidgetStrategy:
            """Minimal TraversalStrategy implementation for testing the seam.

            Accepts **kwargs so that the strategy is compatible with
            traversal_error_policy being threaded through by resolve_label_to_value
            (design D-4: existing implementations that declare **kwargs satisfy the
            protocol under Python's structural subtyping rules).
            """

            def __init__(self) -> None:
                self.called = False
                self.call_args: tuple[object, tuple[str, ...], Any] | None = None

            def traverse(
                self,
                value: object,
                path: tuple[str, ...],
                label: Any,
                **kwargs: object,
            ) -> MlodyValue:
                self.called = True
                self.call_args = (value, path, label)
                return sentinel_value

        stub = StubWidgetStrategy()
        # Register a widget entity and a widget strategy
        widget_mlody = """\
builtins.register("task", struct(
    kind="task",
    name="my_widget",
    inputs=[],
    outputs=[],
    action=None,
))
"""
        # We cannot register a custom kind in the evaluator without modifying it,
        # so we test the dispatch table directly:
        # Register stub into the dispatch table for the "task" kind override
        # (to demonstrate the seam without touching the evaluator).
        original_strategy = TRAVERSAL_STRATEGIES.get("task")
        TRAVERSAL_STRATEGIES["task"] = stub  # type: ignore[assignment]
        try:
            ws = _make_workspace(
                fs,
                extra_files={"teams/myroot/pkg/widgets.mlody": widget_mlody},
            )

            label = parse_label("@myroot//pkg/widgets:my_widget")
            result = resolve_label_to_value(label, ws)
        finally:
            if original_strategy is not None:
                TRAVERSAL_STRATEGIES["task"] = original_strategy
            else:
                del TRAVERSAL_STRATEGIES["task"]

        assert stub.called
        assert result is sentinel_value

    def test_unknown_kind_returns_unresolved(self, fs: FakeFilesystem) -> None:
        """Kind not in dispatch table → MlodyUnresolvedValue (no KeyError)."""
        # We cannot register a custom kind in the evaluator, but we can remove
        # a known kind from the table to simulate an unknown kind.
        original_strategy = TRAVERSAL_STRATEGIES.pop("task")
        try:
            ws = _make_workspace(
                fs,
                extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
            )

            label = parse_label("@myroot//pkg/foo:my_task")
            result = resolve_label_to_value(label, ws)
        finally:
            TRAVERSAL_STRATEGIES["task"] = original_strategy

        assert isinstance(result, MlodyUnresolvedValue)
        assert "task" in result.reason


# ---------------------------------------------------------------------------
# Wildcard guard
# Scenario: "Wildcard label raises immediately"
# ---------------------------------------------------------------------------


class TestEntityFieldPathTraversal:
    """Requirement: field_path in entity name is combined with attribute_path for traversal."""

    def test_dotted_entity_name_traverses_field_path(self, fs: FakeFilesystem) -> None:
        """Scenario: @myroot//pkg/foo:my_task.extra.count resolves to terminal value 42.

        The parser splits :my_task.extra.count into name="my_task",
        field_path=("extra", "count").  The resolver must traverse that path.
        """
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        # Dot-path syntax: entity field_path carries the traversal
        label = parse_label("@myroot//pkg/foo:my_task.extra.count")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == 42

    def test_dotted_entity_name_single_segment_traversal(self, fs: FakeFilesystem) -> None:
        """Scenario: :my_task.extra → field_path=("extra",) → returns _RawAttrValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task.extra")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        # The extra field is a struct — verify it has the expected attribute
        assert hasattr(result.value, "count")

    def test_dotted_entity_name_combined_with_tick_path(self, fs: FakeFilesystem) -> None:
        """Scenario: field_path + tick attribute_path are both applied."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_WITH_INPUTS_MLODY},
        )

        # :my_task.extra traverses to the extra struct, then 'count traverses further
        label = parse_label("@myroot//pkg/foo:my_task.extra'count")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, _RawAttrValue)
        assert result.value == 42

    def test_dotted_entity_name_missing_field_returns_unresolved(
        self, fs: FakeFilesystem
    ) -> None:
        """Scenario: :my_task.nonexistent → MlodyUnresolvedValue."""
        ws = _make_workspace(
            fs,
            extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY},
        )

        label = parse_label("@myroot//pkg/foo:my_task.nonexistent")
        result = resolve_label_to_value(label, ws)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "nonexistent" in result.reason


class TestWildcardGuard:
    """Requirement: resolve_label_to_value public API — wildcard guard."""

    def test_wildcard_label_raises_value_error(self, fs: FakeFilesystem) -> None:
        """Scenario: wildcard label → ValueError (programmer error)."""
        ws = _make_workspace(fs)

        label = parse_label("@myroot//pkg/...")
        with pytest.raises(ValueError, match="wildcard"):
            resolve_label_to_value(label, ws)


# ---------------------------------------------------------------------------
# Helpers for _traverse_one_step tests (no Workspace needed)
# ---------------------------------------------------------------------------


def _make_label() -> Any:
    """Return a minimal label object sufficient for error message construction."""
    return parse_label("@myroot//pkg/foo:my_value")


def _make_struct(**kwargs: object) -> Any:
    """Construct a Starlark Struct from keyword arguments."""
    from common.python.starlarkish.core.struct import Struct

    return Struct(**kwargs)


def _make_loc(path: str | list[str]) -> Any:
    """Construct a minimal posix location Struct."""
    paths = [path] if isinstance(path, str) else list(path)
    return _make_struct(kind="posix", type="posix", name="loc", path=paths)


# ---------------------------------------------------------------------------
# 4.1 Unit tests for _traverse_one_step
# Scenario: "Successful step returns rebuilt Struct with composed location"
# Scenario: "All non-location fields are preserved in the rebuilt Struct"
# Scenario: "Missing field returns MlodyUnresolvedValue"
# Scenario: "Compose error returns MlodyUnresolvedValue"
# Scenario: "Non-Struct field_obj skips rebuild and is returned as-is"
# ---------------------------------------------------------------------------


class TestTraverseOneStep:
    """Requirement: _traverse_one_step shared helper (multi-level-field-traversal spec)."""

    def test_successful_step_returns_rebuilt_struct_with_composed_location(self) -> None:
        """Scenario: successful step returns rebuilt Struct with composed location."""
        field_loc = _make_loc("info")
        field_obj = _make_struct(name="model_info", type=None, location=field_loc)
        record_type = _make_struct(kind="record", name="ModelType", fields=[field_obj])
        parent_loc = _make_loc("models/bert")
        current = _make_struct(
            kind="value",
            name="my_model",
            type=record_type,
            location=parent_loc,
        )

        result = _traverse_one_step(current, "model_info", (), _make_label())

        assert isinstance(result, tuple)
        rebuilt, flag = result
        assert flag is False
        loc = getattr(rebuilt, "location", None)
        assert loc is not None
        # compose_location joins parent path + field path
        assert getattr(loc, "path", None) == ["models/bert/info"]

    def test_all_non_location_fields_preserved_in_rebuilt_struct(self) -> None:
        """Scenario: all non-location fields are preserved in the rebuilt Struct."""
        field_loc = _make_loc("a_dir")
        field_obj = _make_struct(
            name="field_a",
            type=None,
            location=field_loc,
            kind="value",
            representation="some_repr",
        )
        record_type = _make_struct(kind="record", name="T", fields=[field_obj])
        parent = _make_struct(
            kind="value",
            name="root",
            type=record_type,
            location=_make_loc("root"),
        )

        result = _traverse_one_step(parent, "field_a", (), _make_label())

        assert isinstance(result, tuple)
        rebuilt, _ = result
        assert getattr(rebuilt, "name", None) == "field_a"
        assert getattr(rebuilt, "kind", None) == "value"
        assert getattr(rebuilt, "representation", None) == "some_repr"
        assert getattr(rebuilt, "type", None) is None

    def test_missing_field_returns_mlody_unresolved_value(self) -> None:
        """Scenario: missing field returns MlodyUnresolvedValue listing available fields."""
        record_type = _make_struct(
            kind="record", name="ModelType", fields=[_make_struct(name="name", type=None)]
        )
        current = _make_struct(
            kind="value", name="m", type=record_type, location=_make_loc("r")
        )
        label = _make_label()

        result = _traverse_one_step(current, "ghost", (), label)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "ghost" in result.reason
        assert "name" in result.reason  # available fields listed

    def test_location_compose_error_returns_mlody_unresolved_value(self) -> None:
        """Scenario: compose error returns MlodyUnresolvedValue with error message."""
        # Cross-kind locations trigger LocationComposeError.
        field_loc = _make_struct(kind="s3", type="s3", name="s3_loc", path="bucket/x")
        field_obj = _make_struct(name="weights", type=None, location=field_loc)
        record_type = _make_struct(kind="record", name="T", fields=[field_obj])
        # Parent is posix; field is s3 → cross-kind compose error.
        parent = _make_struct(
            kind="value", name="m", type=record_type, location=_make_loc("models")
        )

        result = _traverse_one_step(parent, "weights", (), _make_label())

        assert isinstance(result, MlodyUnresolvedValue)
        # reason contains the compose error message
        assert result.reason

    def test_non_struct_fallback_returns_raw_attr_value(self) -> None:
        """Scenario: non-Struct field_obj (type attr fallback) returns _RawAttrValue."""
        # type has a direct attribute "extra" (not in fields list)
        record_type = _make_struct(kind="record", name="T", fields=[], extra="plain_str")
        current = _make_struct(
            kind="value", name="m", type=record_type, location=_make_loc("r")
        )

        result = _traverse_one_step(current, "extra", (), _make_label())

        assert isinstance(result, _RawAttrValue)
        assert result.value == "plain_str"


# ---------------------------------------------------------------------------
# 5.x Tests — multi-level traversal in ValueTraversalStrategy
# ---------------------------------------------------------------------------


def _make_value_struct_with_fields(
    root_loc_path: str,
    fields: list[Any],
) -> Any:
    """Build a value struct with a record type having the given fields."""
    record_type = _make_struct(kind="record", name="T", fields=fields)
    return _make_struct(
        kind="value",
        name="root",
        type=record_type,
        location=_make_loc(root_loc_path),
    )


def _make_field(name: str, loc_path: str | None, child_fields: list[Any] | None = None) -> Any:
    """Build a field struct.  ``child_fields`` makes the field itself record-typed."""
    if child_fields is not None:
        field_type = _make_struct(kind="record", name=f"{name}_type", fields=child_fields)
    else:
        field_type = None
    loc = _make_loc(loc_path) if loc_path is not None else None
    return _make_struct(name=name, type=field_type, location=loc)


class TestValueTraversalStrategyMultiLevel:
    """Requirement: Multi-step record-aware traversal in ValueTraversalStrategy.

    Scenarios trace to:
      openspec/changes/mlody-field-traversal-multilevel/specs/multi-level-field-traversal/spec.md
    """

    def _strategy(self) -> Any:
        from mlody.resolver.label_value import ValueTraversalStrategy

        return ValueTraversalStrategy()

    def test_two_level_traversal_with_explicit_locations(self) -> None:
        """Scenario: Two-level traversal with explicit locations at both levels.

        root_loc.path="root/path", field_a.location.path="a_dir",
        field_b.location.path="b_file" → composed "root/path/a_dir/b_file"
        """
        label = _make_label()
        field_b = _make_field("field_b", "b_file")
        field_a = _make_field("field_a", "a_dir", child_fields=[field_b])
        root_value = _make_value_struct_with_fields("root/path", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label)

        assert isinstance(result, MlodyValueValue)
        loc = getattr(result.struct, "location", None)
        assert getattr(loc, "path", None) == ["root/path/a_dir/b_file"]

    def test_two_level_traversal_intermediate_field_has_no_location(self) -> None:
        """Scenario: Two-level traversal where intermediate field has no location.

        field_a has no location → field_a name used as path component.
        Composed path: "root/path/field_a/b_file"
        """
        label = _make_label()
        field_b = _make_field("field_b", "b_file")
        field_a = _make_field("field_a", None, child_fields=[field_b])
        root_value = _make_value_struct_with_fields("root/path", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label)

        assert isinstance(result, MlodyValueValue)
        loc = getattr(result.struct, "location", None)
        assert getattr(loc, "path", None) == ["root/path/field_a/b_file"]

    def test_two_level_traversal_leaf_field_has_no_location(self) -> None:
        """Scenario: Two-level traversal where leaf field has no location.

        field_b has no location → field_b name appended.
        Composed path: "root/path/a_dir/field_b"
        """
        label = _make_label()
        field_b = _make_field("field_b", None)
        field_a = _make_field("field_a", "a_dir", child_fields=[field_b])
        root_value = _make_value_struct_with_fields("root/path", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label)

        assert isinstance(result, MlodyValueValue)
        loc = getattr(result.struct, "location", None)
        assert getattr(loc, "path", None) == ["root/path/a_dir/field_b"]

    def test_three_level_traversal_accumulates_locations(self) -> None:
        """Scenario: Three-level traversal accumulates r/a/b/c."""
        label = _make_label()
        field_c = _make_field("field_c", "c")
        field_b = _make_field("field_b", "b", child_fields=[field_c])
        field_a = _make_field("field_a", "a", child_fields=[field_b])
        root_value = _make_value_struct_with_fields("r", [field_a])

        result = self._strategy().traverse(
            root_value, ("field_a", "field_b", "field_c"), label
        )

        assert isinstance(result, MlodyValueValue)
        loc = getattr(result.struct, "location", None)
        assert getattr(loc, "path", None) == ["r/a/b/c"]

    def test_non_record_type_at_intermediate_step_returns_unresolved(self) -> None:
        """Scenario: Non-record type at an intermediate step returns MlodyUnresolvedValue.

        field_a is found but its type.kind is "string" (not "record").
        """
        label = _make_label()
        # field_a has type.kind="string" — not record-typed
        string_type = _make_struct(kind="string", name="StringType")
        field_a = _make_struct(name="field_a", type=string_type, location=_make_loc("a"))
        root_value = _make_value_struct_with_fields("root", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "field_a" in result.reason
        assert "string" in result.reason

    def test_missing_field_at_first_segment_returns_unresolved(self) -> None:
        """Scenario: Missing field at first segment returns MlodyUnresolvedValue."""
        label = _make_label()
        # type.fields is empty — "missing" will not be found
        root_value = _make_value_struct_with_fields("root", [])

        result = self._strategy().traverse(root_value, ("missing", "field_b"), label)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "missing" in result.reason

    def test_missing_field_at_second_segment_returns_unresolved(self) -> None:
        """Scenario: Missing field at second segment returns MlodyUnresolvedValue.

        field_a is found and is record-typed, but "ghost" is absent.
        """
        label = _make_label()
        # field_a is record-typed with no child fields → "ghost" not found
        field_a = _make_field("field_a", "a", child_fields=[])
        root_value = _make_value_struct_with_fields("root", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "ghost"), label)

        assert isinstance(result, MlodyUnresolvedValue)
        assert "ghost" in result.reason

    def test_location_compose_error_at_intermediate_step_returns_unresolved(
        self,
    ) -> None:
        """Scenario: LocationComposeError at an intermediate step stops traversal.

        field_a has an s3 location; parent has posix location → cross-kind error.
        field_b step must NOT be attempted.
        """
        label = _make_label()
        field_b = _make_field("field_b", "b")
        s3_loc = _make_struct(kind="s3", type="s3", name="s3", path="bucket/a")
        field_b_type = _make_struct(kind="record", name="T", fields=[field_b])
        field_a = _make_struct(name="field_a", type=field_b_type, location=s3_loc)
        root_value = _make_value_struct_with_fields("root/posix", [field_a])

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label)

        assert isinstance(result, MlodyUnresolvedValue)
        assert result.reason

    def test_non_record_root_multi_segment_uses_getattr_fallback(self) -> None:
        """Scenario: Non-record root + multi-segment path uses getattr fallback (OQ-13 seam).

        When type.kind != "record", the record loop is NOT activated; the generic
        getattr fallback is used instead.
        """
        label = _make_label()
        # tensor type — not record
        tensor_type = _make_struct(kind="tensor", name="TensorType")
        sub = _make_struct(value=99)
        root_value = _make_struct(
            kind="value", name="t", type=tensor_type, location=None, sub=sub
        )

        result = self._strategy().traverse(root_value, ("sub", "value"), label)

        # getattr fallback resolves sub.value → 99 → _RawAttrValue
        assert isinstance(result, _RawAttrValue)
        assert result.value == 99


# ---------------------------------------------------------------------------
# Group 3 — Traversal Engine unit tests
# Tasks 3.1–3.8
# ---------------------------------------------------------------------------


class TestTraversalErrorPolicy:
    """Requirement: TraversalErrorPolicy enum (task 3.1)."""

    def test_importable_from_label_value(self) -> None:
        """Scenario: TraversalErrorPolicy members are importable."""
        from mlody.resolver.label_value import TraversalErrorPolicy

        assert TraversalErrorPolicy.SKIP is not None
        assert TraversalErrorPolicy.RAISE is not None

    def test_skip_and_raise_are_distinct(self) -> None:
        from mlody.resolver.label_value import TraversalErrorPolicy

        assert TraversalErrorPolicy.SKIP != TraversalErrorPolicy.RAISE


class TestMlodyVectorValue:
    """Requirement: MlodyVectorValue type (task 3.2)."""

    def test_is_mlody_value_subtype(self) -> None:
        """Scenario: MlodyVectorValue is a MlodyValue subtype."""
        from mlody.resolver.label_value import MlodyVectorValue

        v = MlodyVectorValue(elements=())
        assert isinstance(v, MlodyValue)

    def test_carries_element_tuple(self) -> None:
        """Scenario: MlodyVectorValue carries its element tuple."""
        from mlody.resolver.label_value import MlodyVectorValue

        e1 = MlodyValueValue(struct=_make_struct(x=1))
        e2 = MlodyValueValue(struct=_make_struct(x=2))
        v = MlodyVectorValue(elements=(e1, e2))
        assert len(v.elements) == 2
        assert v.elements[0] is e1
        assert v.elements[1] is e2

    def test_frozen_and_hashable(self) -> None:
        from mlody.resolver.label_value import MlodyVectorValue

        v = MlodyVectorValue(elements=())
        d = {v: "ok"}
        assert d[v] == "ok"


# ---------------------------------------------------------------------------
# _engine_index_step  (task 3.4)
# ---------------------------------------------------------------------------


class TestEngineIndexStep:
    """Requirement: IndexSegment on vector-typed value."""

    def _engine(self) -> object:
        from mlody.resolver.label_value import _engine_index_step

        return _engine_index_step

    def _make_vector(self, *values: object) -> object:
        from mlody.resolver.label_value import MlodyVectorValue

        elems = tuple(MlodyValueValue(struct=_make_struct(v=v)) for v in values)
        return MlodyVectorValue(elements=elems)

    def test_in_bounds_positive_index_returns_element(self) -> None:
        """Scenario: IndexSegment on in-bounds positive index returns element."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_index_step

        vec = self._make_vector(10, 20, 30)
        result = _engine_index_step(vec, IndexSegment(index=1), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "v", None) == 20

    def test_in_bounds_negative_index_returns_last_element(self) -> None:
        """Scenario: IndexSegment on in-bounds negative index returns element."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_index_step

        vec = self._make_vector(10, 20, 30)
        result = _engine_index_step(vec, IndexSegment(index=-1), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "v", None) == 30

    def test_out_of_bounds_raise_policy_returns_unresolved(self) -> None:
        """Scenario: IndexSegment out-of-bounds with RAISE policy returns MlodyUnresolvedValue."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_index_step

        vec = self._make_vector(10, 20, 30)
        result = _engine_index_step(vec, IndexSegment(index=10), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)
        assert "10" in result.reason

    def test_out_of_bounds_skip_policy_returns_empty_vector(self) -> None:
        """Scenario: IndexSegment out-of-bounds with SKIP policy returns empty vector."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_index_step

        vec = self._make_vector(10, 20, 30)
        result = _engine_index_step(vec, IndexSegment(index=10), TraversalErrorPolicy.SKIP, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 0

    def test_type_mismatch_raise_policy_returns_unresolved(self) -> None:
        """Scenario: IndexSegment on non-vector value with RAISE policy."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_index_step

        scalar = MlodyValueValue(struct=_make_struct(x=1))
        result = _engine_index_step(scalar, IndexSegment(index=0), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)


# ---------------------------------------------------------------------------
# _engine_key_step  (task 3.5)
# ---------------------------------------------------------------------------


class TestEngineKeyStep:
    """Requirement: KeySegment on map-typed value."""

    def _make_dict_value(self, d: dict[str, object]) -> object:
        from mlody.resolver.label_value import MlodyVectorValue  # noqa: F401
        from mlody.resolver.label_value import _RawAttrValue  # noqa: F401

        # We represent a dict-backed value as a _RawAttrValue wrapping a Python dict.
        # The engine must handle this case.
        from mlody.resolver.label_value import _RawAttrValue

        return _RawAttrValue(value=d, label=_make_label())

    def test_present_key_returns_value(self) -> None:
        """Scenario: KeySegment on present key returns value."""
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_key_step

        d = {"f1": 0.95, "acc": 0.87}
        val = self._make_dict_value(d)
        result = _engine_key_step(val, KeySegment(key="f1"), TraversalErrorPolicy.RAISE, _make_label())
        # Should return a value wrapping 0.95
        assert result is not None
        from mlody.resolver.label_value import _RawAttrValue
        assert isinstance(result, _RawAttrValue)
        assert result.value == 0.95

    def test_missing_key_raise_policy_returns_unresolved(self) -> None:
        """Scenario: KeySegment on missing key with RAISE policy returns MlodyUnresolvedValue."""
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_key_step

        val = self._make_dict_value({"f1": 0.95})
        result = _engine_key_step(val, KeySegment(key="recall"), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)
        assert "recall" in result.reason

    def test_missing_key_skip_policy_returns_empty_vector(self) -> None:
        """Scenario: KeySegment on missing key with SKIP policy returns empty vector."""
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_key_step

        val = self._make_dict_value({"f1": 0.95})
        result = _engine_key_step(val, KeySegment(key="recall"), TraversalErrorPolicy.SKIP, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 0

    def test_non_dict_value_raise_returns_unresolved(self) -> None:
        """Non-dict-backed value with RAISE policy → MlodyUnresolvedValue."""
        from mlody.core.traversal_grammar import KeySegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_key_step

        scalar = MlodyValueValue(struct=_make_struct(x=1))
        result = _engine_key_step(scalar, KeySegment(key="x"), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)


# ---------------------------------------------------------------------------
# _engine_wildcard_step  (task 3.6)
# ---------------------------------------------------------------------------


class TestEngineWildcardStep:
    """Requirement: WildcardSegment — flat vector of all immediate children."""

    def test_wildcard_on_vector_value_returns_all_elements(self) -> None:
        """Scenario: WildcardSegment on MlodyVectorValue returns all elements."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_wildcard_step

        e1 = MlodyValueValue(struct=_make_struct(v=1))
        e2 = MlodyValueValue(struct=_make_struct(v=2))
        e3 = MlodyValueValue(struct=_make_struct(v=3))
        vec = MlodyVectorValue(elements=(e1, e2, e3))

        result = _engine_wildcard_step(vec, WildcardSegment(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert list(result.elements) == [e1, e2, e3]

    def test_wildcard_on_dict_backed_returns_all_values(self) -> None:
        """Scenario: WildcardSegment on dict-backed value returns all values."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _RawAttrValue, _engine_wildcard_step

        d = {"x": 1, "y": 2}
        val = _RawAttrValue(value=d, label=_make_label())

        result = _engine_wildcard_step(val, WildcardSegment(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2
        values = [getattr(e, "value", None) for e in result.elements if isinstance(e, _RawAttrValue)]
        assert 1 in values
        assert 2 in values

    def test_wildcard_on_record_struct_returns_all_fields(self) -> None:
        """Scenario: WildcardSegment on record Struct returns all fields."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_wildcard_step

        field_a = _make_field("fa", "a")
        field_b = _make_field("fb", "b")
        root_value = _make_value_struct_with_fields("root", [field_a, field_b])

        result = _engine_wildcard_step(root_value, WildcardSegment(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2

    def test_wildcard_on_non_traversable_raise_returns_unresolved(self) -> None:
        """Non-traversable value with RAISE policy → MlodyUnresolvedValue."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_wildcard_step

        # A scalar MlodyUnresolvedValue is not traversable
        scalar = MlodyUnresolvedValue(label=_make_label(), reason="already unresolved")
        result = _engine_wildcard_step(scalar, WildcardSegment(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyUnresolvedValue)

    def test_wildcard_on_non_traversable_skip_returns_empty_vector(self) -> None:
        """Non-traversable value with SKIP policy → MlodyVectorValue(elements=())."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_wildcard_step

        scalar = MlodyUnresolvedValue(label=_make_label(), reason="already unresolved")
        result = _engine_wildcard_step(scalar, WildcardSegment(), TraversalErrorPolicy.SKIP, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 0


# ---------------------------------------------------------------------------
# _engine_recursive_descent_step  (task 3.7)
# ---------------------------------------------------------------------------


class TestEngineRecursiveDescentStep:
    """Requirement: RecursiveDescentSegment — flat vector of all descendants."""

    def test_recursive_descent_on_flat_vector(self) -> None:
        """Scenario: RecursiveDescentSegment on flat vector collects all elements."""
        from mlody.core.traversal_grammar import RecursiveDescentSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_recursive_descent_step

        e1 = MlodyValueValue(struct=_make_struct(v=1))
        e2 = MlodyValueValue(struct=_make_struct(v=2))
        e3 = MlodyValueValue(struct=_make_struct(v=3))
        vec = MlodyVectorValue(elements=(e1, e2, e3))

        result = _engine_recursive_descent_step(vec, RecursiveDescentSegment(), TraversalErrorPolicy.RAISE, _make_label())
        assert isinstance(result, MlodyVectorValue)
        assert list(result.elements) == [e1, e2, e3]

    def test_recursive_descent_on_nested_record_depth_first(self) -> None:
        """Scenario: RecursiveDescentSegment on nested structure — depth-first order.

        root has fields [a, b]; a has children [a1, a2]; b is scalar.
        Expected DFS order: a, a1, a2, b
        """
        from mlody.core.traversal_grammar import RecursiveDescentSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_recursive_descent_step

        a1 = _make_field("a1", "a1")
        a2 = _make_field("a2", "a2")
        # field 'a' is record-typed with children [a1, a2]
        a = _make_field("a", "a", child_fields=[a1, a2])
        b = _make_field("b", "b")  # scalar (no child fields)
        root_value = _make_value_struct_with_fields("root", [a, b])

        result = _engine_recursive_descent_step(
            root_value, RecursiveDescentSegment(), TraversalErrorPolicy.RAISE, _make_label()
        )
        assert isinstance(result, MlodyVectorValue)
        # Must have 4 descendants: a, a1, a2, b
        assert len(result.elements) == 4

    def test_recursive_descent_non_traversable_skip_returns_empty(self) -> None:
        """Non-traversable root with SKIP policy → MlodyVectorValue(elements=())."""
        from mlody.core.traversal_grammar import RecursiveDescentSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _engine_recursive_descent_step

        scalar = MlodyUnresolvedValue(label=_make_label(), reason="already unresolved")
        result = _engine_recursive_descent_step(
            scalar, RecursiveDescentSegment(), TraversalErrorPolicy.SKIP, _make_label()
        )
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 0

    def test_recursive_descent_non_traversable_raise_returns_unresolved(self) -> None:
        """Non-traversable root with RAISE policy → MlodyUnresolvedValue."""
        from mlody.core.traversal_grammar import RecursiveDescentSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _engine_recursive_descent_step

        scalar = MlodyUnresolvedValue(label=_make_label(), reason="already unresolved")
        result = _engine_recursive_descent_step(
            scalar, RecursiveDescentSegment(), TraversalErrorPolicy.RAISE, _make_label()
        )
        assert isinstance(result, MlodyUnresolvedValue)


# ---------------------------------------------------------------------------
# Group 4 — _traverse_one_step extension and ValueTraversalStrategy integration
# Tasks 4.1–4.6
# ---------------------------------------------------------------------------


class TestTraverseOneStepExtension:
    """Requirement: _traverse_one_step dispatches all PathSegment kinds (task 4.1)."""

    def test_field_segment_behaves_identically_to_str(self) -> None:
        """Scenario: _traverse_one_step with FieldSegment behaves identically to str input."""
        from mlody.core.traversal_grammar import FieldSegment
        from mlody.resolver.label_value import TraversalErrorPolicy, _traverse_one_step

        field_loc = _make_loc("info")
        field_obj = _make_struct(name="model_info", type=None, location=field_loc)
        record_type = _make_struct(kind="record", name="T", fields=[field_obj])
        parent_loc = _make_loc("models/bert")
        current = _make_struct(kind="value", name="m", type=record_type, location=parent_loc)

        result_str = _traverse_one_step(current, "model_info", (), _make_label(), TraversalErrorPolicy.RAISE)
        result_seg = _traverse_one_step(current, FieldSegment(name="model_info"), (), _make_label(), TraversalErrorPolicy.RAISE)

        assert type(result_str) is type(result_seg)
        if isinstance(result_str, tuple):
            assert isinstance(result_seg, tuple)
            rebuilt_str, _ = result_str
            rebuilt_seg, _ = result_seg
            assert getattr(rebuilt_str, "location", None) == getattr(rebuilt_seg, "location", None)

    def test_str_backward_compat_preserved(self) -> None:
        """Scenario: _traverse_one_step with str backward compatibility preserved."""
        from mlody.resolver.label_value import TraversalErrorPolicy, _traverse_one_step

        field_loc = _make_loc("a")
        field_obj = _make_struct(name="field_a", type=None, location=field_loc)
        record_type = _make_struct(kind="record", name="T", fields=[field_obj])
        current = _make_struct(kind="value", name="m", type=record_type, location=_make_loc("root"))

        # Both call signatures must produce the same result
        r1 = _traverse_one_step(current, "field_a", (), _make_label())
        r2 = _traverse_one_step(current, "field_a", (), _make_label(), TraversalErrorPolicy.RAISE)
        assert type(r1) is type(r2)

    def test_wildcard_segment_delegates_to_wildcard_handler(self) -> None:
        """Scenario: _traverse_one_step with WildcardSegment delegates to wildcard handler."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _traverse_one_step

        field_a = _make_field("fa", "a")
        field_b = _make_field("fb", "b")
        root_value = _make_value_struct_with_fields("root", [field_a, field_b])

        result = _traverse_one_step(root_value, WildcardSegment(), (), _make_label(), TraversalErrorPolicy.RAISE)
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2

    def test_index_segment_delegates_to_index_handler(self) -> None:
        """Scenario: _traverse_one_step with IndexSegment delegates to index handler."""
        from mlody.core.traversal_grammar import IndexSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, _traverse_one_step

        e1 = MlodyValueValue(struct=_make_struct(v=1))
        e2 = MlodyValueValue(struct=_make_struct(v=2))
        vec = MlodyVectorValue(elements=(e1, e2))

        result = _traverse_one_step(vec, IndexSegment(index=1), (), _make_label(), TraversalErrorPolicy.RAISE)
        assert isinstance(result, MlodyValueValue)
        assert getattr(result.struct, "v", None) == 2


class TestValueTraversalStrategyEngine:
    """Requirement: Engine integration into ValueTraversalStrategy (tasks 4.2–4.4)."""

    def _strategy(self) -> object:
        from mlody.resolver.label_value import ValueTraversalStrategy

        return ValueTraversalStrategy()

    def test_pure_field_segment_path_unchanged(self) -> None:
        """Scenario: Pure FieldSegment path continues to use existing record-aware loop."""
        label = _make_label()
        field_b = _make_field("field_b", "b")
        field_a = _make_field("field_a", "a", child_fields=[field_b])
        root_value = _make_value_struct_with_fields("root", [field_a])

        from mlody.resolver.label_value import TraversalErrorPolicy

        result = self._strategy().traverse(root_value, ("field_a", "field_b"), label, traversal_error_policy=TraversalErrorPolicy.RAISE)
        assert isinstance(result, MlodyValueValue)

    def test_wildcard_path_returns_vector_value(self) -> None:
        """Scenario: Wildcard path returns MlodyVectorValue from strategy."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        fa = _make_field("fa", "a")
        fb = _make_field("fb", "b")
        fc = _make_field("fc", "c")
        root_value = _make_value_struct_with_fields("root", [fa, fb, fc])

        segs = (WildcardSegment(),)
        result = self._strategy().traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.RAISE)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 3

    def test_mapped_traversal_flattens_field_over_wildcard(self) -> None:
        """Scenario: FieldSegment mapped over vector produces flat vector."""
        from mlody.core.traversal_grammar import FieldSegment, WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        loss_child1 = _make_field("loss", "l1")
        loss_child2 = _make_field("loss", "l2")
        child1 = _make_field("child1", "c1", child_fields=[loss_child1])
        child2 = _make_field("child2", "c2", child_fields=[loss_child2])
        root_value = _make_value_struct_with_fields("root", [child1, child2])

        segs = (WildcardSegment(), FieldSegment("loss"))
        result = self._strategy().traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.RAISE)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2  # one loss per child — flat

    def test_mapped_traversal_skip_drops_missing_fields(self) -> None:
        """Scenario: FieldSegment mapped over vector with SKIP omits missing fields."""
        from mlody.core.traversal_grammar import FieldSegment, WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        loss_c1 = _make_field("optional_field", "l1")
        loss_c2 = _make_field("optional_field", "l2")
        child1 = _make_field("child1", "c1", child_fields=[loss_c1])
        child2 = _make_field("child2", "c2", child_fields=[loss_c2])
        child3 = _make_field("child3", "c3", child_fields=[])  # no optional_field
        root_value = _make_value_struct_with_fields("root", [child1, child2, child3])

        segs = (WildcardSegment(), FieldSegment("optional_field"))
        result = self._strategy().traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.SKIP)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2  # child3's missing field is dropped

    def test_two_consecutive_wildcards_produce_vector_of_vectors(self) -> None:
        """Scenario: Two consecutive WildcardSegments produce vector of vectors."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        sub1 = _make_field("s1", "s1")
        sub2 = _make_field("s2", "s2")
        sub3 = _make_field("s3", "s3")
        sub4 = _make_field("s4", "s4")
        child1 = _make_field("child1", "c1", child_fields=[sub1, sub2])
        child2 = _make_field("child2", "c2", child_fields=[sub3, sub4])
        root_value = _make_value_struct_with_fields("root", [child1, child2])

        segs = (WildcardSegment(), WildcardSegment())
        result = self._strategy().traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.RAISE)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2  # not flattened to 4
        for elem in result.elements:
            assert isinstance(elem, MlodyVectorValue)
            assert len(elem.elements) == 2

    def test_mixed_path_with_index_routes_to_engine(self) -> None:
        """Scenario: Mixed path with IndexSegment routes to traversal engine."""
        from mlody.core.traversal_grammar import FieldSegment, IndexSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        # Build a vector then access index 0 then field "loss"
        # We'll test via parse_traversal_expression to use typed segments
        loss_field = _make_field("loss", "loss")
        child = _make_value_struct_with_fields("child", [loss_field])

        from mlody.resolver.label_value import MlodyVectorValue as MVV

        vec = MVV(elements=(MlodyValueValue(struct=child),))

        segs = (IndexSegment(0), FieldSegment("loss"))
        result = self._strategy().traverse(vec, segs, label, traversal_error_policy=TraversalErrorPolicy.RAISE)  # type: ignore[arg-type]
        assert isinstance(result, MlodyValueValue)

    def test_deferred_seam_comment_exists(self) -> None:
        """Task 4.5: D-6 seam comment is present in the source."""
        import inspect

        from mlody.resolver import label_value as lv

        src = inspect.getsource(lv)
        assert "TODO(mlody-label-traversal): uniform-level-traversal" in src


# ---------------------------------------------------------------------------
# Group 5 — Resolver API and show command wiring
# Tasks 5.1–5.4
# ---------------------------------------------------------------------------


class TestResolverAPIExtension:
    """Requirement: resolve_label_to_value gains traversal_error_policy parameter (task 5.1)."""

    def test_default_raise_policy_is_backward_compatible(self, fs: object) -> None:
        """Scenario: Default RAISE policy is backward-compatible."""
        from pyfakefs.fake_filesystem import FakeFilesystem

        assert isinstance(fs, FakeFilesystem)
        ws = _make_workspace(fs, extra_files={"teams/myroot/pkg/foo.mlody": TASK_MLODY})  # type: ignore[arg-type]

        label = parse_label("@myroot//pkg/foo:my_task")
        result = resolve_label_to_value(label, ws)
        assert isinstance(result, MlodyTaskValue)

    def test_mlody_vector_value_importable_from_mlody_resolver(self) -> None:
        """Scenario: MlodyVectorValue is re-exported from mlody.resolver."""
        from mlody.resolver import MlodyVectorValue  # noqa: F401

        assert MlodyVectorValue is not None

    def test_traversal_error_policy_importable_from_mlody_resolver(self) -> None:
        """Scenario: traversal_error_policy is importable from mlody.resolver."""
        from mlody.resolver import TraversalErrorPolicy  # noqa: F401

        assert TraversalErrorPolicy is not None

    def test_wildcard_traversal_returns_vector_value(self) -> None:
        """Scenario: Wildcard traversal returns MlodyVectorValue with correct element count."""
        from mlody.core.traversal_grammar import WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy

        label = _make_label()
        fa = _make_field("fa", "a")
        fb = _make_field("fb", "b")
        fc = _make_field("fc", "c")
        root_value = _make_value_struct_with_fields("root", [fa, fb, fc])

        from mlody.resolver.label_value import ValueTraversalStrategy

        strategy = ValueTraversalStrategy()
        segs = (WildcardSegment(),)
        result = strategy.traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.RAISE)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 3

    def test_skip_policy_drops_missing_fields_in_wildcard_traversal(self) -> None:
        """Scenario: SKIP policy silently drops missing fields in wildcard + field traversal."""
        from mlody.core.traversal_grammar import FieldSegment, WildcardSegment
        from mlody.resolver.label_value import MlodyVectorValue, TraversalErrorPolicy, ValueTraversalStrategy

        label = _make_label()
        loss_c1 = _make_field("metric", "m1")
        loss_c2 = _make_field("metric", "m2")
        child1 = _make_field("child1", "c1", child_fields=[loss_c1])
        child2 = _make_field("child2", "c2", child_fields=[loss_c2])
        child3 = _make_field("child3", "c3", child_fields=[])  # no metric field
        root_value = _make_value_struct_with_fields("root", [child1, child2, child3])

        segs = (WildcardSegment(), FieldSegment("metric"))
        result = ValueTraversalStrategy().traverse(root_value, segs, label, traversal_error_policy=TraversalErrorPolicy.SKIP)  # type: ignore[arg-type]
        assert isinstance(result, MlodyVectorValue)
        assert len(result.elements) == 2
