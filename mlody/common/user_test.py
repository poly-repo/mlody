"""Integration tests for mlody/common/user.mlody."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
import mlody

assert mlody.__name__ == "mlody"

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_USER_MLODY = (_THIS_DIR / "user.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/user.mlody": _USER_MLODY,
}

_PREAMBLE = (
    'load("//mlody/common/types.mlody")\n'
    'load("//mlody/common/user.mlody")\n'
)


def _eval(extra_mlody: str) -> Evaluator:
    script = _PREAMBLE + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    return ev


def test_user_registers_with_kind_user() -> None:
    ev = _eval(
        'user(name="agarcia", description="Ava Garcia", groups=["framera", "framera-admin"])\n'
    )
    assert "agarcia" in ev.registry.users.by_name
    user_value = ev.registry.users.by_name["agarcia"]
    assert user_value.kind == "user"
    assert user_value.name == "agarcia"


def test_user_stores_description_and_groups() -> None:
    ev = _eval(
        'user(name="jlee", description="Jordan Lee", groups=["sonora"])\n'
    )
    user_value = ev.registry.users.by_name["jlee"]
    assert user_value.description == "Jordan Lee"
    assert user_value.groups == ["sonora"]


def test_user_stores_avatar_when_provided() -> None:
    ev = _eval(
        'user(name="agarcia", description="Ava Garcia", groups=["framera"], '
        'avatar="assets/images/avatars/avatars-1-0.png")\n'
    )
    user_value = ev.registry.users.by_name["agarcia"]
    assert user_value.avatar == "assets/images/avatars/avatars-1-0.png"


def test_user_groups_is_mandatory() -> None:
    with pytest.raises(ValueError, match="Missing mandatory argument"):
        _eval('user(name="mcollins", description="Maya Collins")\n')


def test_user_groups_rejects_non_string_entries() -> None:
    with pytest.raises(TypeError, match="each element must be a string"):
        _eval('user(name="kchen", description="Kira Chen", groups=["pixelle", 1])\n')


def test_user_avatar_rejects_non_string_value() -> None:
    with pytest.raises(TypeError, match="expects type 'string'"):
        _eval('user(name="kchen", description="Kira Chen", groups=["pixelle"], avatar=1)\n')


def test_user_attaches_declared_entity_type() -> None:
    ev = _eval(
        'user(name="jramirez", description="Julia Ramirez", groups=["sonora", "sonora-admin"])\n'
    )
    user_value = ev.registry.users.by_name["jramirez"]
    assert user_value._entity_type is not None
    assert user_value._entity_type.name == "mlody-user"
