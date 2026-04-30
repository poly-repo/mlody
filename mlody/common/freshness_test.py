"""Integration tests for mlody/common/freshness.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_FRESHNESS_MLODY = (_THIS_DIR / "freshness.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/freshness.mlody": _FRESHNESS_MLODY,
}


def _eval(extra_mlody: str) -> Evaluator:
    script = 'load("//mlody/common/freshness.mlody")\n' + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def test_manual_bare_returns_freshness_struct() -> None:
    ev = _eval("result = manual()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "freshness"
    assert result.type == "manual"
    assert result.abstract is False


def test_manual_bare_validator_accepts_arbitrary_values() -> None:
    ev = _eval("result = manual()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("anything") is True
    assert result.validator(42) is True


def test_ttl_requires_duration_and_stores_it() -> None:
    ev = _eval('result = ttl(duration="P1D")')
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "freshness"
    assert result.type == "ttl"
    assert result.attributes["duration"] == "P1D"


def test_ttl_missing_duration_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Missing mandatory argument"):
        _eval("result = ttl()")


def test_ttl_rejects_non_string_duration() -> None:
    with pytest.raises(TypeError):
        _eval("result = ttl(duration=1)")


def test_scheduled_requires_schedule_and_stores_it() -> None:
    ev = _eval('result = scheduled(schedule="0 * * * *")')
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "freshness"
    assert result.type == "scheduled"
    assert result.attributes["schedule"] == "0 * * * *"


def test_scheduled_missing_schedule_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Missing mandatory argument"):
        _eval("result = scheduled()")


def test_scheduled_rejects_non_string_schedule() -> None:
    with pytest.raises(TypeError):
        _eval("result = scheduled(schedule=5)")


def test_always_bare_returns_freshness_struct() -> None:
    ev = _eval("result = always()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "freshness"
    assert result.type == "always"
    assert result.abstract is False
