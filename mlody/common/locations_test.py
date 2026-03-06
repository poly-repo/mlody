"""Integration tests for mlody/common/locations.mlody."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
}


def _eval(extra_mlody: str) -> Evaluator:
    script = 'load("//mlody/common/locations.mlody")\n' + dedent(extra_mlody)
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


# ---------------------------------------------------------------------------
# TC-001 / TC-010: s3() returns correct struct fields
# ---------------------------------------------------------------------------


def test_s3_bare_returns_location_struct_with_correct_kind() -> None:
    """TC-001: s3() returns a struct with kind='location'."""
    ev = _eval("result = s3()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "location"


def test_s3_bare_returns_struct_with_type_s3() -> None:
    """TC-010: s3() returns a struct with type='s3'."""
    ev = _eval("result = s3()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.type == "s3"


def test_s3_bare_is_not_abstract() -> None:
    """TC-001: s3() struct has abstract=False."""
    ev = _eval("result = s3()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.abstract is False


def test_s3_bare_validator_accepts_any_value() -> None:
    """TC-001: bare s3() validator accepts arbitrary values (no constraints)."""
    ev = _eval("result = s3()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    # Bare s3 has no constraints; validator should accept any value
    assert result.validator("anything") is True
    assert result.validator(42) is True


# ---------------------------------------------------------------------------
# TC-002: s3(bucket="b") validator behaviour
# ---------------------------------------------------------------------------


def test_s3_with_bucket_validator_accepts_matching_value() -> None:
    """TC-002: s3(bucket='b').validator('b') passes."""
    ev = _eval("result = s3(bucket='b')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("b") is True


def test_s3_with_bucket_validator_rejects_other_value() -> None:
    """TC-002: s3(bucket='b').validator('other') raises ValueError."""
    ev = _eval("result = s3(bucket='b')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    with pytest.raises(ValueError):
        result.validator("other")


# ---------------------------------------------------------------------------
# TC-003: s3(region="us-east-1") validator behaviour
# ---------------------------------------------------------------------------


def test_s3_with_region_validator_accepts_matching_value() -> None:
    """TC-003: s3(region='us-east-1').validator('us-east-1') passes."""
    ev = _eval("result = s3(region='us-east-1')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("us-east-1") is True


def test_s3_with_region_validator_rejects_other_value() -> None:
    """TC-003: s3(region='us-east-1').validator('us-west-2') raises ValueError."""
    ev = _eval("result = s3(region='us-east-1')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    with pytest.raises(ValueError):
        result.validator("us-west-2")


# ---------------------------------------------------------------------------
# TC-004 / TC-011: posix() returns correct struct fields
# ---------------------------------------------------------------------------


def test_posix_bare_returns_location_struct_with_correct_kind() -> None:
    """TC-004: posix() returns a struct with kind='location'."""
    ev = _eval("result = posix()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "location"


def test_posix_bare_returns_struct_with_type_posix() -> None:
    """TC-011: posix() returns a struct with type='posix'."""
    ev = _eval("result = posix()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.type == "posix"


def test_posix_bare_is_not_abstract() -> None:
    """TC-004: posix() struct has abstract=False."""
    ev = _eval("result = posix()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.abstract is False


def test_posix_bare_validator_accepts_any_value() -> None:
    """TC-010/TC-011: bare posix() validator accepts arbitrary values."""
    ev = _eval("result = posix()")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("/any/path") is True
    assert result.validator("anything") is True


# ---------------------------------------------------------------------------
# TC-005: posix(path="/data") validator behaviour
# ---------------------------------------------------------------------------


def test_posix_with_path_validator_accepts_matching_value() -> None:
    """TC-005: posix(path='/data').validator('/data') passes."""
    ev = _eval("result = posix(path='/data')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("/data") is True


def test_posix_with_path_validator_rejects_other_value() -> None:
    """TC-005: posix(path='/data').validator('/other') raises ValueError."""
    ev = _eval("result = posix(path='/data')")
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    with pytest.raises(ValueError):
        result.validator("/other")


# ---------------------------------------------------------------------------
# TC-006: user-defined location with base registers and injects factory
# ---------------------------------------------------------------------------


def test_child_location_registers_with_correct_kind_and_type() -> None:
    """TC-006: location(name='team_s3', base=s3(bucket='prod')) registers correctly."""
    ev = _eval("""\
        location(name="team_s3", base=s3(bucket="prod"))
        result = team_s3()
    """)
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.kind == "location"
    assert result.type == "team_s3"


# ---------------------------------------------------------------------------
# TC-007: child location inherits parent attrs
# ---------------------------------------------------------------------------


def test_child_location_inherits_parent_attrs() -> None:
    """TC-007: team_s3 inherits 'bucket' from s3 without redeclaring it."""
    ev = _eval("""\
        location(name="team_s3", base=s3(bucket="prod"))
        result = team_s3(bucket="x")
    """)
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    # bucket is inherited — the factory call should succeed and return a struct
    assert result.kind == "location"


# ---------------------------------------------------------------------------
# TC-008: conflicting attr declaration raises ValueError
# ---------------------------------------------------------------------------


def test_child_location_with_conflicting_attr_raises_value_error() -> None:
    """TC-008: redeclaring 'bucket' in a child of s3 raises ValueError."""
    with pytest.raises(ValueError, match="conflict"):
        _eval("""\
            location(
                name="bad_s3",
                base=s3(),
                attrs={"bucket": attr(type="string", mandatory=False)},
            )
        """)


# ---------------------------------------------------------------------------
# TC-009: predicate is enforced
# ---------------------------------------------------------------------------


def test_location_predicate_is_enforced() -> None:
    """TC-009: location with predicate=lambda rejects values that fail it."""
    ev = _eval("""\
        location(
            name="pred_s3",
            base=s3(),
            predicate=lambda v: v != "bad",
        )
        result = pred_s3()
    """)
    result = ev._module_globals[ev.root_path / "test.mlody"]["result"]
    assert result.validator("good") is True
    with pytest.raises((ValueError, Exception)):
        result.validator("bad")


# ---------------------------------------------------------------------------
# TC-012: unknown kwarg to factory raises TypeError
# ---------------------------------------------------------------------------


def test_s3_factory_rejects_unknown_kwarg() -> None:
    """TC-012: s3(nonexistent='x') raises TypeError."""
    with pytest.raises(TypeError):
        _eval("result = s3(nonexistent='x')")
