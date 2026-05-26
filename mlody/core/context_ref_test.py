"""Tests for ContextRef sentinel and cfg global."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from common.python.starlarkish.core.struct import Struct
from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

from mlody.core.context_ref import ContextRef, build_cfg_struct

_THIS_DIR = Path(__file__).parent
_COMMON = _THIS_DIR.parent / "common"

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": (_THIS_DIR / "rule.mlody").read_text(),
    "mlody/common/attrs.mlody": (_COMMON / "attrs.mlody").read_text(),
    "mlody/common/types.mlody": (_COMMON / "types.mlody").read_text(),
    "mlody/common/freshness.mlody": (_COMMON / "freshness.mlody").read_text(),
    "mlody/common/locations.mlody": (_COMMON / "locations.mlody").read_text(),
    "mlody/common/representation.mlody": (_COMMON / "representation.mlody").read_text(),
    "mlody/common/values.mlody": (_COMMON / "values.mlody").read_text(),
}


def _eval_with_cfg(extra_mlody: str) -> Evaluator:
    script = (
        'load("//mlody/common/types.mlody")\n'
        'load("//mlody/common/locations.mlody")\n'
        'load("//mlody/common/representation.mlody")\n'
        'load("//mlody/common/values.mlody")\n'
        + dedent(extra_mlody)
    )
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev._persistent_injections["cfg"] = build_cfg_struct()
        ev.eval_file(root / "test.mlody")
    return ev


# ---------------------------------------------------------------------------
# ContextRef dataclass
# ---------------------------------------------------------------------------


class TestContextRef:
    def test_source_attribute(self) -> None:
        ref = ContextRef("cfg.sha", "workspace.commit")
        assert ref.source == "cfg.sha"

    def test_ctx_path_attribute(self) -> None:
        ref = ContextRef("cfg.sha", "workspace.commit")
        assert ref.ctx_path == "workspace.commit"

    def test_class_level_sentinel_present(self) -> None:
        ref = ContextRef("cfg.user", "workspace.user")
        assert hasattr(ref, "_is_context_ref")
        assert ContextRef._is_context_ref is True

    def test_sentinel_not_a_dataclass_field(self) -> None:
        assert "_is_context_ref" not in ContextRef.__dataclass_fields__

    def test_frozen(self) -> None:
        ref = ContextRef("cfg.branch", "workspace.branch")
        try:
            ref.source = "oops"  # type: ignore[misc]
            assert False, "should be frozen"
        except Exception:
            pass

    def test_equality_based_on_source_and_ctx_path(self) -> None:
        assert ContextRef("cfg.sha", "workspace.commit") == ContextRef("cfg.sha", "workspace.commit")
        assert ContextRef("cfg.sha", "workspace.commit") != ContextRef("cfg.user", "workspace.user")


# ---------------------------------------------------------------------------
# build_cfg_struct
# ---------------------------------------------------------------------------


class TestBuildCfgStruct:
    def test_returns_struct(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg, Struct)

    def test_workspace_is_struct(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace, Struct)

    def test_sha_is_context_ref_with_correct_paths(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.sha, ContextRef)
        assert cfg.workspace.sha.source == "cfg.workspace.sha"
        assert cfg.workspace.sha.ctx_path == "workspace.commit"

    def test_user_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.user, ContextRef)
        assert cfg.workspace.user.source == "cfg.workspace.user"
        assert cfg.workspace.user.ctx_path == "workspace.user"

    def test_branch_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.branch, ContextRef)
        assert cfg.workspace.branch.ctx_path == "workspace.branch"

    def test_directory_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.directory, ContextRef)
        assert cfg.workspace.directory.ctx_path == "workspace.directory"

    def test_modified_files_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.modified_files, ContextRef)
        assert cfg.workspace.modified_files.ctx_path == "workspace.modified_files"

    def test_untracked_files_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.workspace.untracked_files, ContextRef)
        assert cfg.workspace.untracked_files.ctx_path == "workspace.untracked_files"

    def test_run_id_is_context_ref(self) -> None:
        cfg = build_cfg_struct()
        assert isinstance(cfg.run_id, ContextRef)
        assert cfg.run_id.ctx_path == "run.id"


# ---------------------------------------------------------------------------
# value() with cfg.sha default — ContextRef stored verbatim, no validation
# ---------------------------------------------------------------------------


class TestValueDefaultFromCfg:
    def test_cfg_sha_default_stored_as_context_ref(self) -> None:
        ev = _eval_with_cfg('value(name="x", default=cfg.workspace.sha)')
        v = ev.registry.values.by_name["x"]
        assert isinstance(v.default, ContextRef)
        assert v.default.source == "cfg.workspace.sha"

    def test_cfg_user_default_stored_as_context_ref(self) -> None:
        ev = _eval_with_cfg('value(name="y", default=cfg.workspace.user)')
        v = ev.registry.values.by_name["y"]
        assert isinstance(v.default, ContextRef)
        assert v.default.source == "cfg.workspace.user"

    def test_cfg_default_with_type_does_not_raise(self) -> None:
        ev = _eval_with_cfg(
            'value(name="z", type=string(), default=cfg.workspace.branch)'
        )
        v = ev.registry.values.by_name["z"]
        assert isinstance(v.default, ContextRef)

    def test_plain_string_default_still_works(self) -> None:
        ev = _eval_with_cfg('value(name="a", default="hello")')
        v = ev.registry.values.by_name["a"]
        assert v.default == "hello"
        assert not isinstance(v.default, ContextRef)
