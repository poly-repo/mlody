"""Tests for the public setf API skeleton."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from starlarkish.core.struct import Struct

from mlody.core.setf import can_setf, resolve_places, setf, setf_root
from mlody.core.setf_strategies import StructFieldSetter
from mlody.core.traversal_grammar import FieldSegment, PathExpression
from mlody.core.virtual_value import make_virtual_value
from mlody.core.workspace import Workspace


_STRING_TYPE = Struct(kind="type", type="string", name="string")
_WORKSPACE_INFO_TYPE = Struct(
    kind="type",
    type="workspace_info",
    name="workspace_info",
    _root_kind="record",
    fields=[
        Struct(name="branch", type=_STRING_TYPE),
        Struct(name="sha", type=_STRING_TYPE),
    ],
)

_WORKSPACE_ROOT = Path("/workspace")
_BUILTINS_MLODY = """\
def root(name, path, description=""):
    builtins.register("root", struct(
        name=name,
        path=path,
        description=description,
    ))
"""
_ROOTS_MLODY = """\
load("//mlody/core/builtins.mlody", "root")

root(name="lexica", path="//mlody/teams/lexica", description="team root")
"""
_TYPES_MLODY = """\
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


def _create_workspace_project(
    fs: FakeFilesystem,
    root: Path,
    *,
    api_sha: str = "old-api",
    web_sha: str = "old-web",
) -> Path:
    fs.create_file(str(root / "mlody/core/builtins.mlody"), contents=_BUILTINS_MLODY)
    fs.create_file(str(root / "mlody/roots.mlody"), contents=_ROOTS_MLODY)
    fs.create_file(str(root / "mlody/common/types.mlody"), contents=_TYPES_MLODY)
    fs.create_file(
        str(root / "mlody/teams/lexica/services/release/api/image.mlody"),
        contents=f"""\
builtins.register("value", Struct(
    kind="value",
    name="image",
    config=Struct(commit_sha="{api_sha}"),
    _lineage=[],
))
""",
    )
    fs.create_file(
        str(root / "mlody/teams/lexica/services/release/web/image.mlody"),
        contents=f"""\
builtins.register("value", Struct(
    kind="value",
    name="image",
    config=Struct(commit_sha="{web_sha}"),
    _lineage=[],
))
""",
    )
    fs.create_file(
        str(root / "mlody/teams/lexica/services/broken/image.mlody"),
        contents="""\
builtins.register("value", Struct(
    kind="value",
    name="image",
    config=Struct(version="1"),
    _lineage=[],
))
""",
    )
    return root


@pytest.fixture()
def workspace_project(fs: FakeFilesystem) -> Path:
    return _create_workspace_project(fs, _WORKSPACE_ROOT)


@pytest.fixture()
def loaded_workspace(workspace_project: Path) -> Workspace:
    ws = Workspace(monorepo_root=workspace_project)
    ws.load()
    return ws


def _virtual_workspace_info() -> Struct:
    return make_virtual_value(
        value_type=_WORKSPACE_INFO_TYPE,
        label="'info",
        materializer=lambda _value: Struct(branch="main", sha="abc123"),
    )


class TestSetfModuleSkeleton:
    """Foundation tests for the public setf API surface."""

    def test_resolve_places_parses_string_selector_for_struct_fields(self) -> None:
        """Task 2.2 / 2.3: string selectors reuse the traversal parser."""
        root = Struct(config=Struct(learning_rate=0.1))

        place_set = resolve_places(root, ".config.learning_rate")

        assert len(place_set.places) == 1
        place = place_set.places[0]
        assert str(place.selector) == ".config.learning_rate"
        assert place.accessor == ".config.learning_rate"
        assert place.current_value == 0.1
        assert isinstance(place.strategy, StructFieldSetter)

    def test_resolve_places_accepts_path_expression_without_reparsing(self) -> None:
        """Task 2.2: PathExpression selectors are accepted directly."""
        selector = PathExpression(
            segments=(FieldSegment("config"), FieldSegment("learning_rate"))
        )
        root = Struct(config=Struct(learning_rate=0.1))

        place_set = resolve_places(root, selector)

        assert len(place_set.places) == 1
        assert place_set.places[0].selector == selector

    def test_resolve_places_marks_slice_selection_as_projected(self) -> None:
        """Task 3.4: slice selections resolve to one projected place."""
        root = Struct(items=[0, 1, 2, 3])

        place_set = resolve_places(root, ".items[::2]")

        assert len(place_set.places) == 1
        place = place_set.places[0]
        assert place.projected is True
        assert place.accessor == ".items[::2]"
        assert place.current_value == [0, 2]

    def test_resolve_places_expands_wildcard_into_multiple_direct_places(self) -> None:
        """Task 3.5: wildcard expansion returns one place per child."""
        root = Struct(outputs=Struct(left=1, right=2))

        place_set = resolve_places(root, ".outputs[*]")

        assert [place.accessor for place in place_set.places] == [
            ".outputs.left",
            ".outputs.right",
        ]

    def test_resolve_places_expands_recursive_descent_into_matching_places(self) -> None:
        """Task 3.6: recursive descent returns all matching descendants."""
        root = Struct(
            first=Struct(sha="a"),
            second=Struct(inner=Struct(sha="b")),
        )

        place_set = resolve_places(root, "...sha")

        assert [place.accessor for place in place_set.places] == [
            ".first.sha",
            ".second.inner.sha",
        ]

    def test_resolve_places_traverses_declared_virtual_fields(self) -> None:
        """Task 6.1: virtual values expose declared child places to setf."""
        root = _virtual_workspace_info()

        place_set = resolve_places(root, ".branch")

        assert len(place_set.places) == 1
        place = place_set.places[0]
        assert place.accessor == ".branch"
        assert getattr(place.current_value, "kind", None) == "value"
        assert getattr(getattr(place.current_value, "location", None), "type", None) == "virtual"
        assert getattr(place.current_value, "label", None) == "'info.branch"

    def test_resolve_places_expands_recursive_descent_through_virtual_values(self) -> None:
        """Task 6.1: recursive descent can enumerate declared virtual children."""
        root = Struct(info=_virtual_workspace_info())

        place_set = resolve_places(root, "...branch")

        assert [place.accessor for place in place_set.places] == [".info.branch"]

    def test_can_setf_accepts_valid_direct_struct_field_assignment(self) -> None:
        """Task 4.3: can_setf preflights a valid direct field write."""
        root = Struct(config=Struct(learning_rate=0.1))

        can_setf(root, ".config.learning_rate", 0.2)

    def test_can_setf_rejects_virtual_value_targets_until_strategy_exists(self) -> None:
        """Task 6.1: virtual targets fail explicitly during write preflight."""
        root = _virtual_workspace_info()

        with pytest.raises(NotImplementedError, match="virtual value selectors"):
            can_setf(root, ".branch", "release")

    def test_setf_updates_direct_struct_field_without_mutating_original(self) -> None:
        """Task 3.1 / 4.4: direct Struct field writes rebuild the path safely."""
        root = Struct(config=Struct(learning_rate=0.1))

        updated = setf_root(root, ".config.learning_rate", 0.2)

        assert updated.config.learning_rate == 0.2
        assert root.config.learning_rate == 0.1

    def test_setf_updates_list_index_inside_struct(self) -> None:
        """Task 3.2 / 4.4: direct list index writes are supported."""
        root = Struct(items=[1, 2, 3])

        updated = setf_root(root, ".items[1]", 99)

        assert updated.items == [1, 99, 3]
        assert root.items == [1, 2, 3]

    def test_setf_updates_nested_dict_key_path(self) -> None:
        """Task 3.3 / 4.4: direct dict key writes are supported."""
        root = {"config": {"learning_rate": 0.1}}

        updated = setf_root(root, '["config"]["learning_rate"]', 0.2)

        assert updated["config"]["learning_rate"] == 0.2
        assert root["config"]["learning_rate"] == 0.1

    def test_setf_updates_projected_slice_without_mutating_original(self) -> None:
        """Task 3.4 / 4.4: projected slice writes update every selected index."""
        root = Struct(items=[0, 1, 2, 3, 4])

        updated = setf_root(root, ".items[::2]", 42)

        assert updated.items == [42, 1, 42, 3, 42]
        assert root.items == [0, 1, 2, 3, 4]

    def test_setf_updates_all_places_selected_by_recursive_descent(self) -> None:
        """Task 3.6 / 4.4: recursive-descent bulk writes update all matches."""
        root = Struct(
            first=Struct(sha="old"),
            second=Struct(inner=Struct(sha="old")),
        )

        updated = setf_root(root, "...sha", "new")

        assert updated.first.sha == "new"
        assert updated.second.inner.sha == "new"
        assert root.first.sha == "old"
        assert root.second.inner.sha == "old"

    def test_setf_fails_before_any_write_on_heterogeneous_bulk_targets(self) -> None:
        """Task 4.5 / 4.6: bulk writes abort before commit on mixed contracts."""
        root = Struct(
            values=[
                Struct(type="integer", representation="plain", payload=1),
                Struct(type="string", representation="plain", payload="x"),
            ]
        )

        with pytest.raises(ValueError, match="uniform declared type"):
            setf_root(root, ".values[*]", Struct(payload="new"))

        assert root.values[0].payload == 1
        assert root.values[1].payload == "x"

    def test_setf_appends_lineage_to_direct_selected_value(self) -> None:
        """Task 5.3 / 5.4: direct writes append lineage to the updated value."""
        root = Struct(config=Struct(value=1, _lineage=[]))
        replacement = Struct(value=2, _lineage=[])

        updated = setf_root(
            root,
            ".config",
            replacement,
            author="tester",
            reason="bump",
            timestamp="2026-04-20T00:00:00Z",
        )

        assert len(updated.config._lineage) == 1
        event = updated.config._lineage[0]
        assert event.accessor == ".config"
        assert event.new_value == replacement

    def test_setf_appends_projected_lineage_to_aggregate_owner(self) -> None:
        """Task 5.5: projected writes preserve the aggregate accessor in lineage."""
        root = {"items": [0, 1, 2, 3], "_lineage": []}

        updated = setf_root(
            root,
            '["items"][::2]',
            42,
            author="tester",
            reason="mask",
            timestamp="2026-04-20T00:00:00Z",
        )

        assert updated["items"] == [42, 1, 42, 3]
        assert len(updated["_lineage"]) == 1
        event = updated["_lineage"][0]
        assert event.accessor == '["items"][::2]'

    def test_resolve_places_raises_for_missing_struct_field(self) -> None:
        """Task 3.7: missing field selections fail immediately."""
        root = Struct(config=Struct(learning_rate=0.1))

        with pytest.raises(AttributeError):
            resolve_places(root, ".config.missing")

    def test_setf_raises_for_out_of_bounds_index(self) -> None:
        """Task 3.7: missing index targets fail immediately."""
        root = Struct(items=[1, 2])

        with pytest.raises(IndexError):
            setf_root(root, ".items[5]", 99)

    def test_setf_raises_for_missing_dict_key(self) -> None:
        """Task 3.7: missing dict-key targets fail immediately."""
        root = {"config": {"learning_rate": 0.1}}

        with pytest.raises(KeyError):
            setf_root(root, '["config"]["missing"]', 0.2)


class TestWorkspaceFirstSetf:
    """Workspace-first label-aware `setf` acceptance tests."""

    def test_setf_updates_unqualified_label_against_explicit_workspace(
        self, loaded_workspace: Workspace
    ) -> None:
        updated_workspace = setf(
            "@lexica//services/release/api/image:image.config.commit_sha",
            "new-api",
            workspace=loaded_workspace,
        )

        assert updated_workspace is loaded_workspace
        assert (
            updated_workspace.resolve(
                "@lexica//services/release/api/image:image.config.commit_sha"
            )
            == "new-api"
        )

    def test_setf_uses_cwd_workspace_for_unqualified_labels(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace_project: Path,
    ) -> None:
        monkeypatch.chdir(workspace_project)

        updated_workspace = setf(
            "@lexica//services/release/api/image:image.config.commit_sha",
            "cwd-sha",
        )

        assert (
            updated_workspace.resolve(
                "@lexica//services/release/api/image:image.config.commit_sha"
            )
            == "cwd-sha"
        )

    def test_setf_rejects_explicit_workspace_qualifiers(
        self, loaded_workspace: Workspace
    ) -> None:
        with pytest.raises(ValueError, match="relative to the current workspace"):
            setf(
                "main|@lexica//services/release/api/image:image.config.commit_sha",
                "resolved-sha",
                workspace=loaded_workspace,
            )

    def test_setf_uses_expand_wildcard_label_and_preserves_transactionality(
        self,
        loaded_workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        real_expand = loaded_workspace.expand_wildcard_label

        def _record_expand(inner_label: str) -> list[str]:
            calls.append(inner_label)
            _ = real_expand
            return [
                "@lexica//services/release/api/image:image.config.commit_sha",
                "@lexica//services/release/web/image:image.config.commit_sha",
                "@lexica//services/broken/image:image.config.commit_sha",
            ]

        monkeypatch.setattr(loaded_workspace, "expand_wildcard_label", _record_expand)

        with pytest.raises(AttributeError):
            setf(
                "@lexica//services/...:image.config.commit_sha",
                "bulk-sha",
                workspace=loaded_workspace,
            )

        assert calls == ["@lexica//services/...:image.config.commit_sha"]
        assert (
            loaded_workspace.resolve(
                "@lexica//services/release/api/image:image.config.commit_sha"
            )
            == "old-api"
        )
        assert (
            loaded_workspace.resolve(
                "@lexica//services/release/web/image:image.config.commit_sha"
            )
            == "old-web"
        )

    def test_setf_lowers_entity_field_path_into_root_engine(
        self, loaded_workspace: Workspace
    ) -> None:
        setf(
            "@lexica//services/release/web/image:image.config.commit_sha",
            "field-path-sha",
            workspace=loaded_workspace,
        )

        entity = loaded_workspace.resolve("@lexica//services/release/web/image:image")
        assert entity.config.commit_sha == "field-path-sha"

    def test_setf_rejects_workspace_virtual_attributes_as_writable_targets(
        self, loaded_workspace: Workspace
    ) -> None:
        with pytest.raises(NotImplementedError, match="workspace attribute"):
            setf("'info.branch", "release", workspace=loaded_workspace)
