"""Tests for mlody/common/config.py RegisteredConfig dataclass."""

from __future__ import annotations

import pytest

from mlody.common._registered_struct import _wrapper_for_kind
from mlody.common.config import RegisteredConfig
from mlody.common.struct import Struct


# ---------------------------------------------------------------------------
# TC-8.2: populate_from_struct with minimal struct
# ---------------------------------------------------------------------------


def test_registered_config_minimal_struct() -> None:
    """RegisteredConfig accepts a struct with only name and empty rules.

    Ref: Scenario 'populate_from_struct with minimal struct'.
    """
    s = Struct(kind="config", name="x", description="", rules={})
    cfg = RegisteredConfig(s)
    assert cfg.name == "x"
    assert cfg.description == ""
    assert cfg.rules == {}


# ---------------------------------------------------------------------------
# TC-8.3: populate_from_struct with full struct
# ---------------------------------------------------------------------------


def test_registered_config_full_struct() -> None:
    """RegisteredConfig preserves description and mixed-type rules values.

    Ref: Scenario 'populate_from_struct with full struct'.
    """
    rules = {":lr": 0.001, ":epochs": 10, ":flag": True, ":tag": "v1", ":mask": None}
    s = Struct(kind="config", name="team", description="Team defaults", rules=rules)
    cfg = RegisteredConfig(s)
    assert cfg.name == "team"
    assert cfg.description == "Team defaults"
    assert cfg.rules[":lr"] == 0.001
    assert cfg.rules[":epochs"] == 10
    assert cfg.rules[":flag"] is True
    assert cfg.rules[":tag"] == "v1"
    assert cfg.rules[":mask"] is None


# ---------------------------------------------------------------------------
# TC-8.4: wrong kind raises ValueError
# ---------------------------------------------------------------------------


def test_registered_config_wrong_kind_raises() -> None:
    """RegisteredConfig raises ValueError when struct has kind != 'config'.

    Ref: Scenario 'wrong kind raises ValueError'.
    """
    s = Struct(kind="task", name="x", rules={})
    with pytest.raises(ValueError, match="config"):
        RegisteredConfig(s)


# ---------------------------------------------------------------------------
# TC-8.5: _wrapper_for_kind("config") returns RegisteredConfig
# ---------------------------------------------------------------------------


def test_wrapper_for_kind_config_returns_registered_config() -> None:
    """_wrapper_for_kind('config') dispatches to RegisteredConfig.

    Ref: Scenario '_wrapper_for_kind dispatch'.
    """
    assert _wrapper_for_kind("config") is RegisteredConfig
