"""Integration tests for mlody/common/config.mlody."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_CONFIG_MLODY = (_THIS_DIR / "config.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/config.mlody": _CONFIG_MLODY,
}

_PREAMBLE = 'load("//mlody/common/config.mlody", "config")\n'


def _eval(extra_mlody: str) -> Evaluator:
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


# ---------------------------------------------------------------------------
# TC-9.2: minimal config registers correctly
# ---------------------------------------------------------------------------


def test_config_minimal_registers_correctly() -> None:
    """config(name='x', rules={}) registers struct with correct fields.

    Ref: Scenario 'Minimal config registers correctly'.
    """
    ev = _eval('config(name="x", rules={})\n')
    assert "x" in ev.registry.configs.by_name
    cfg = ev.registry.configs.by_name["x"]
    assert getattr(cfg, "kind") == "config"
    assert getattr(cfg, "name") == "x"
    assert getattr(cfg, "description") == ""
    assert getattr(cfg, "rules") == {}


# ---------------------------------------------------------------------------
# TC-9.3: description is preserved
# ---------------------------------------------------------------------------


def test_config_description_preserved() -> None:
    """config(name='x', description='d', rules={}) preserves description.

    Ref: Scenario 'config with description'.
    """
    ev = _eval('config(name="x", description="d", rules={})\n')
    cfg = ev.registry.configs.by_name["x"]
    assert getattr(cfg, "description") == "d"


# ---------------------------------------------------------------------------
# TC-9.4: mixed scalar types round-trip correctly
# ---------------------------------------------------------------------------


def test_config_mixed_scalar_rules_roundtrip() -> None:
    """Rules with mixed scalar types round-trip through the evaluator unchanged.

    Ref: Scenario 'config with mixed scalar rules'.
    """
    ev = _eval(
        'config(\n'
        '  name="x",\n'
        '  rules={":lr": 0.001, ":epochs": 10, ":flag": True, ":tag": "v1", ":mask": None},\n'
        ')\n'
    )
    rules = getattr(ev.registry.configs.by_name["x"], "rules")
    assert rules[":lr"] == 0.001
    assert rules[":epochs"] == 10
    assert rules[":flag"] is True
    assert rules[":tag"] == "v1"
    assert rules[":mask"] is None


# ---------------------------------------------------------------------------
# TC-9.5: missing name raises ValueError
# ---------------------------------------------------------------------------


def test_config_missing_name_raises() -> None:
    """config() without name raises ValueError.

    Ref: Scenario 'config without name raises ValueError'.
    """
    with pytest.raises(ValueError, match="name"):
        _eval('config(rules={})\n')


# ---------------------------------------------------------------------------
# TC-9.6: missing rules raises ValueError
# ---------------------------------------------------------------------------


def test_config_missing_rules_raises() -> None:
    """config(name='x') without rules raises ValueError.

    Ref: Scenario 'config without rules raises ValueError'.
    """
    with pytest.raises(ValueError, match="rules"):
        _eval('config(name="x")\n')


# ---------------------------------------------------------------------------
# TC-9.7: non-string key in rules raises ValueError
# ---------------------------------------------------------------------------


def test_config_non_string_key_raises() -> None:
    """config with non-string key in rules raises ValueError.

    Ref: Scenario 'config with non-string key in rules raises ValueError'.
    """
    with pytest.raises(ValueError, match="string"):
        _eval('config(name="x", rules={42: "bad"})\n')
