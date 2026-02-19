"""Tests for mlody.core.targets — target address parsing and resolution."""

from __future__ import annotations

import dataclasses

import pytest

from common.python.starlarkish.core.struct import struct
from mlody.core.targets import TargetAddress, parse_target, resolve_target_value


# ---------------------------------------------------------------------------
# 2.2 TargetAddress: immutability and hashability
# ---------------------------------------------------------------------------


class TestTargetAddressImmutability:
    """Requirement: TargetAddress is a frozen dataclass."""

    def test_frozen_rejects_field_mutation(self) -> None:
        addr = TargetAddress(root="A", package_path="p", target_name="t", field_path=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            addr.root = "B"  # type: ignore[misc]

    def test_equal_instances_share_hash(self) -> None:
        a = TargetAddress(root="R", package_path="p", target_name="t", field_path=("f",))
        b = TargetAddress(root="R", package_path="p", target_name="t", field_path=("f",))
        assert a == b
        assert hash(a) == hash(b)

    def test_usable_as_dict_key(self) -> None:
        addr = TargetAddress(root=None, package_path=None, target_name="x", field_path=())
        d = {addr: 42}
        assert d[addr] == 42


# ---------------------------------------------------------------------------
# 3.2 Parsing: happy paths
# ---------------------------------------------------------------------------


class TestParseTargetHappyPaths:
    """Requirement: Parse fully-qualified target addresses."""

    def test_fully_qualified_with_root_path_target_and_field(self) -> None:
        addr = parse_target("@TEAM_A//models/bert:config.learning_rate")
        assert addr.root == "TEAM_A"
        assert addr.package_path == "models/bert"
        assert addr.target_name == "config"
        assert addr.field_path == ("learning_rate",)

    def test_without_root(self) -> None:
        addr = parse_target("//models/bert:config")
        assert addr.root is None
        assert addr.package_path == "models/bert"
        assert addr.target_name == "config"
        assert addr.field_path == ()

    def test_package_relative(self) -> None:
        addr = parse_target(":config.lr")
        assert addr.root is None
        assert addr.package_path is None
        assert addr.target_name == "config"
        assert addr.field_path == ("lr",)

    def test_multiple_field_segments(self) -> None:
        addr = parse_target("@common//shared:defaults.training.batch_size")
        assert addr.field_path == ("training", "batch_size")

    def test_package_relative_no_fields(self) -> None:
        addr = parse_target(":mytarget")
        assert addr.root is None
        assert addr.package_path is None
        assert addr.target_name == "mytarget"
        assert addr.field_path == ()


# ---------------------------------------------------------------------------
# 3.3 Parsing: malformed input
# ---------------------------------------------------------------------------


class TestParseTargetMalformed:
    """Requirement: Reject malformed target addresses."""

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_target("")

    def test_empty_target_name(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_target("//path/to:")

    def test_missing_colon_separator(self) -> None:
        with pytest.raises(ValueError, match="missing.*':'"):
            parse_target("//path/to/target")

    def test_bare_root_prefix(self) -> None:
        with pytest.raises(ValueError, match="'//'"):
            parse_target("@ROOT")


# ---------------------------------------------------------------------------
# 4.2 Resolution: happy paths
# ---------------------------------------------------------------------------


class TestResolveTargetValueHappyPaths:
    """Requirement: Resolve target addresses to values."""

    def test_resolve_simple_target(self) -> None:
        roots = {"lexica": struct(name="lexica", bert=42)}
        addr = TargetAddress(root="lexica", package_path="models", target_name="bert", field_path=())
        assert resolve_target_value(addr, roots) == 42

    def test_resolve_with_field_traversal(self) -> None:
        roots = {
            "lexica": struct(
                name="lexica",
                bert=struct(config=struct(lr=0.001)),
            ),
        }
        addr = TargetAddress(
            root="lexica",
            package_path="models",
            target_name="bert",
            field_path=("config", "lr"),
        )
        assert resolve_target_value(addr, roots) == 0.001

    def test_resolve_single_field(self) -> None:
        roots = {"r": struct(name="r", t=struct(val=99))}
        addr = TargetAddress(root="r", package_path=None, target_name="t", field_path=("val",))
        assert resolve_target_value(addr, roots) == 99


# ---------------------------------------------------------------------------
# 4.3 Resolution: error paths
# ---------------------------------------------------------------------------


class TestResolveTargetValueErrors:
    """Requirement: Resolve target addresses — error cases."""

    def test_missing_root_raises_key_error(self) -> None:
        roots = {"lexica": struct(name="lexica")}
        addr = TargetAddress(root="UNKNOWN", package_path=None, target_name="t", field_path=())
        with pytest.raises(KeyError, match="UNKNOWN.*available roots.*lexica"):
            resolve_target_value(addr, roots)

    def test_missing_target_on_root_raises_attribute_error(self) -> None:
        roots = {"r": struct(name="r")}
        addr = TargetAddress(root="r", package_path=None, target_name="nonexistent", field_path=())
        with pytest.raises(AttributeError):
            resolve_target_value(addr, roots)

    def test_missing_field_in_path_raises_attribute_error(self) -> None:
        roots = {"r": struct(name="r", t=struct(a=1))}
        addr = TargetAddress(root="r", package_path=None, target_name="t", field_path=("missing",))
        with pytest.raises(AttributeError):
            resolve_target_value(addr, roots)

    def test_root_is_none_raises_key_error(self) -> None:
        roots = {"r": struct(name="r")}
        addr = TargetAddress(root=None, package_path=None, target_name="t", field_path=())
        with pytest.raises(KeyError, match="No root specified"):
            resolve_target_value(addr, roots)
