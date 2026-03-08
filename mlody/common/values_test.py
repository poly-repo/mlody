"""Integration tests for mlody/common/values.mlody."""
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
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
}


def _eval(extra_mlody: str) -> Evaluator:
    script = (
        'load("//mlody/common/types.mlody")\n'
        'load("//mlody/common/locations.mlody")\n'
        'load("//mlody/common/values.mlody")\n'
        + dedent(extra_mlody)
    )
    files = dict(_BASE_FILES)
    files["test.mlody"] = script
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


def _result(ev: Evaluator) -> object:
    return ev._module_globals[ev.root_path / "test.mlody"]["result"]


# ---------------------------------------------------------------------------
# TC-001: value() with direct structs registers with kind="value"
# ---------------------------------------------------------------------------


def test_value_with_direct_structs_registers_correctly() -> None:
    """TC-001: value(name='x', type=integer(), location=s3()) → kind='value'."""
    ev = _eval('value(name="x", type=integer(), location=s3())')
    assert "x" in ev._values_by_name
    v = ev._values_by_name["x"]
    assert v.kind == "value"
    assert v.name == "x"


def test_value_stores_type_and_location_name() -> None:
    """TC-001: value struct holds .type.name and .location.name."""
    ev = _eval('value(name="x", type=integer(), location=s3())')
    v = ev._values_by_name["x"]
    assert v.type.name == "integer"
    assert v.location.name == "s3"


# ---------------------------------------------------------------------------
# TC-002: string label for type is resolved
# ---------------------------------------------------------------------------


def test_value_string_type_label_resolves_to_type_struct() -> None:
    """TC-002: type='integer' (string) resolves to the integer type struct."""
    ev = _eval('value(name="y", type="integer", location=s3())')
    v = ev._values_by_name["y"]
    assert v.type.kind == "type"
    assert v.type.name == "integer"


# ---------------------------------------------------------------------------
# TC-003: string label for location is resolved
# ---------------------------------------------------------------------------


def test_value_string_location_label_resolves_to_location_struct() -> None:
    """TC-003: location='s3' (string) resolves to the s3 location struct."""
    ev = _eval('value(name="z", type=integer(), location="s3")')
    v = ev._values_by_name["z"]
    assert v.location.kind == "location"
    assert v.location.name == "s3"


# ---------------------------------------------------------------------------
# TC-004: constrained type struct is stored
# ---------------------------------------------------------------------------


def test_value_stores_constrained_type_struct() -> None:
    """TC-004: type=integer(max=100) stores the constrained struct."""
    ev = _eval('value(name="bounded", type=integer(max=100), location=s3())')
    v = ev._values_by_name["bounded"]
    assert v.type.kind == "type"
    assert v.type.attributes.get("max") == 100


# ---------------------------------------------------------------------------
# TC-005: constrained location struct is stored
# ---------------------------------------------------------------------------


def test_value_stores_constrained_location_struct() -> None:
    """TC-005: location=s3(bucket='prod') stores the constrained struct."""
    ev = _eval('value(name="prod_data", type=integer(), location=s3(bucket="prod"))')
    v = ev._values_by_name["prod_data"]
    assert v.location.kind == "location"
    assert v.location.attributes.get("bucket") == "prod"


# ---------------------------------------------------------------------------
# TC-006: unknown type string raises NameError
# ---------------------------------------------------------------------------


def test_value_unknown_type_string_raises_name_error() -> None:
    """TC-006: type='nonexistent' raises NameError."""
    with pytest.raises(NameError):
        _eval('value(name="bad", type="nonexistent", location=s3())')


# ---------------------------------------------------------------------------
# TC-007: unknown location string raises NameError
# ---------------------------------------------------------------------------


def test_value_unknown_location_string_raises_name_error() -> None:
    """TC-007: location='nonexistent' raises NameError."""
    with pytest.raises(NameError):
        _eval('value(name="bad", type=integer(), location="nonexistent")')


# ---------------------------------------------------------------------------
# TC-008: wrong type for type attr raises TypeError
# ---------------------------------------------------------------------------


def test_value_location_struct_as_type_raises_type_error() -> None:
    """TC-008: passing a location struct as type raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="bad", type=s3(), location=s3())')


# ---------------------------------------------------------------------------
# TC-009: wrong type for location attr raises TypeError
# ---------------------------------------------------------------------------


def test_value_type_struct_as_location_raises_type_error() -> None:
    """TC-009: passing a type struct as location raises TypeError."""
    with pytest.raises(TypeError):
        _eval('value(name="bad", type=integer(), location=integer())')
