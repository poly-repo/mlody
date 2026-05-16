"""Tests for mlody.core.registry_view — F14: _label_components extraction.

Each test traces back to task 2.7 in
openspec/changes/mlody-refactor-phase-1/tasks.md.

Strategy:
- _label_components is tested directly (unit tests, no evaluator needed).
- match_registry_entity_label and expand_wildcard_label are tested by seeding
  an Evaluator's registry directly (no .mlody file evaluation needed), so the
  tests are pure in-memory and do not require data files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from mlody.core.registry_view import RegistryView, _label_components
from mlody.core.workspace_models import RootInfo
from common.python.starlarkish.core.struct import Struct
from common.python.starlarkish.evaluator.evaluator import Evaluator

# ---------------------------------------------------------------------------
# Root info helpers
# ---------------------------------------------------------------------------

_ROOT_INFOS: dict[str, RootInfo] = {
    "lexica": RootInfo(name="lexica", path="//mlody/teams/lexica", description=""),
    "sonora": RootInfo(name="sonora", path="//mlody/teams/sonora", description=""),
}

_EMPTY_ROOT_INFOS: dict[str, RootInfo] = {}


# ---------------------------------------------------------------------------
# _label_components unit tests  (F14)
# ---------------------------------------------------------------------------


def test_label_components_returns_root_prefix_when_root_in_root_infos() -> None:
    """F14: root_prefix strips leading/trailing slashes from the root_infos path."""
    entity = Struct(root="lexica", path="models/bert")
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix == "mlody/teams/lexica"
    assert path_suffix == "models/bert"


def test_label_components_returns_none_root_prefix_when_root_absent() -> None:
    """F14: root_prefix is None when entity.root is None."""
    entity = Struct(root=None, path="some/path")
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix is None
    assert path_suffix == "some/path"


def test_label_components_returns_none_root_prefix_when_root_not_in_root_infos() -> None:
    """F14: root_prefix is None when entity.root is absent from root_infos."""
    entity = Struct(root="unknown_root", path="some/path")
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix is None
    assert path_suffix == "some/path"


def test_label_components_strips_leading_and_trailing_slashes_from_path() -> None:
    """F14: path_suffix strips leading and trailing slashes from entity.path."""
    entity = Struct(root=None, path="/models/bert/")
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix is None
    assert path_suffix == "models/bert"


def test_label_components_empty_path_suffix_when_path_is_none() -> None:
    """F14: path_suffix is empty string when entity.path is None."""
    entity = Struct(root="lexica", path=None)
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix == "mlody/teams/lexica"
    assert path_suffix == ""


def test_label_components_empty_root_infos_yields_no_prefix() -> None:
    """F14: empty root_infos always yields root_prefix=None."""
    entity = Struct(root="lexica", path="models")
    root_prefix, path_suffix = _label_components(entity, _EMPTY_ROOT_INFOS)

    assert root_prefix is None
    assert path_suffix == "models"


def test_label_components_entity_without_root_or_path_attributes() -> None:
    """F14: entity with no root/path attributes yields (None, '')."""
    entity = Struct(name="myentity")
    root_prefix, path_suffix = _label_components(entity, _ROOT_INFOS)

    assert root_prefix is None
    assert path_suffix == ""


# ---------------------------------------------------------------------------
# RegistryView behavioural tests: same results before/after _label_components
# extraction  (F14)
# ---------------------------------------------------------------------------
# We seed the evaluator registry directly (no .mlody evaluation) to keep
# tests filesystem-free and fast.


def _make_registry_with_tasks(fs: FakeFilesystem) -> RegistryView:
    """Create a RegistryView with two seeded task entries.

    - ("task", "mlody/teams/lexica/pipeline", "train")  — root-prefixed stem
    - ("task", "other/path", "infer")                    — rootless stem

    No .mlody files are evaluated; entries are inserted directly into the
    evaluator's registry so the test avoids data file dependencies.
    """
    project = Path("/workspace")
    fs.create_dir(str(project))
    evaluator = Evaluator(project)
    registry = RegistryView(evaluator)

    task_train = Struct(kind="task", name="train", path="pipeline")
    task_infer = Struct(kind="task", name="infer", path="other/path")

    registry.set_registry_entity(
        ("task", "mlody/teams/lexica/pipeline", "train"),
        task_train,
    )
    registry.set_registry_entity(
        ("task", "other/path", "infer"),
        task_infer,
    )
    return registry


def test_match_registry_entity_label_finds_entity_with_root_prefix(
    fs: FakeFilesystem,
) -> None:
    """F14: match_registry_entity_label resolves a root-prefixed entity.

    After the _label_components extraction the result is identical to
    pre-extraction behaviour.
    """
    registry = _make_registry_with_tasks(fs)

    entity = Struct(
        root="lexica",
        path="pipeline",
        name="train",
        field_path=(),
    )
    root_infos = {
        "lexica": RootInfo(name="lexica", path="//mlody/teams/lexica", description=""),
    }
    anchor = registry.match_registry_entity_label(
        "@lexica//pipeline:train",
        entity=entity,
        entity_query=None,
        attribute_path=None,
        root_infos=root_infos,
    )

    assert anchor is not None
    assert anchor.registry_key[0] == "task"
    assert anchor.registry_key[2] == "train"


def test_match_registry_entity_label_finds_entity_without_root_prefix(
    fs: FakeFilesystem,
) -> None:
    """F14: match_registry_entity_label resolves an entity with no root."""
    registry = _make_registry_with_tasks(fs)

    entity = Struct(
        root=None,
        path="other/path",
        name="infer",
        field_path=(),
    )
    anchor = registry.match_registry_entity_label(
        "//other/path:infer",
        entity=entity,
        entity_query=None,
        attribute_path=None,
        root_infos={},
    )

    assert anchor is not None
    assert anchor.registry_key[0] == "task"
    assert anchor.registry_key[2] == "infer"


def test_expand_wildcard_label_expands_with_root_prefix(
    fs: FakeFilesystem,
) -> None:
    """F14: expand_wildcard_label expands a root-scoped wildcard.

    The wildcard form ``@root//path/...`` returns one label per unique stem
    under that root/path combination.  Result is identical to pre-extraction
    behaviour.
    """
    registry = _make_registry_with_tasks(fs)

    root_infos = {
        "lexica": RootInfo(name="lexica", path="//mlody/teams/lexica", description=""),
    }
    # @lexica//pipeline/... expands to labels under mlody/teams/lexica/pipeline
    expanded = registry.expand_wildcard_label(
        "@lexica//pipeline/...",
        root_infos=root_infos,
    )

    # The seeded task has stem "mlody/teams/lexica/pipeline"; after stripping the
    # root prefix "mlody/teams/lexica" the rel_path is "pipeline", so the label
    # rendered is "@lexica//pipeline".
    assert len(expanded) >= 1
    assert any("@lexica//pipeline" in label for label in expanded)


def test_expand_wildcard_label_expands_without_root_prefix(
    fs: FakeFilesystem,
) -> None:
    """F14: expand_wildcard_label expands a rootless wildcard."""
    registry = _make_registry_with_tasks(fs)

    # //other/path/... expands to labels under the "other/path" stem.
    expanded = registry.expand_wildcard_label(
        "//other/path/...",
        root_infos={},
    )

    # The seeded task has stem "other/path"; rel_path == "other/path".
    assert len(expanded) >= 1
    assert any("other/path" in label for label in expanded)
