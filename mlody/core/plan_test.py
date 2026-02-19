"""Tests for mlody.core.plan — execution plan data model."""

from __future__ import annotations

import dataclasses
import json

import pytest

from mlody.core.plan import Activity, BuildImage, Execute, Plan


# ---------------------------------------------------------------------------
# Activity protocol
# ---------------------------------------------------------------------------


class TestActivityProtocol:
    """Requirement: Activity protocol for extensibility."""

    def test_build_image_satisfies_protocol(self) -> None:
        assert isinstance(BuildImage(), Activity)

    def test_execute_satisfies_protocol(self) -> None:
        assert isinstance(Execute(), Activity)

    def test_object_missing_to_dict_fails_isinstance(self) -> None:
        class NotAnActivity:
            kind: str = "fake"

        assert not isinstance(NotAnActivity(), Activity)

    def test_custom_type_with_kind_and_to_dict_satisfies_protocol(self) -> None:
        class Custom:
            kind: str = "custom"

            def to_dict(self) -> dict[str, object]:
                return {"kind": self.kind}

        assert isinstance(Custom(), Activity)


# ---------------------------------------------------------------------------
# BuildImage
# ---------------------------------------------------------------------------


class TestBuildImage:
    """Requirement: Stub activity types — BuildImage."""

    def test_kind_is_build_image(self) -> None:
        b = BuildImage(image_name="train:latest", dockerfile="Dockerfile.train")
        assert b.kind == "build_image"

    def test_to_dict(self) -> None:
        b = BuildImage(image_name="train:latest", dockerfile="Dockerfile.train")
        assert b.to_dict() == {
            "image_name": "train:latest",
            "dockerfile": "Dockerfile.train",
            "kind": "build_image",
        }

    def test_default_values(self) -> None:
        b = BuildImage()
        assert b.kind == "build_image"
        assert b.image_name == ""
        assert b.dockerfile == ""

    def test_frozen(self) -> None:
        b = BuildImage()
        with pytest.raises(dataclasses.FrozenInstanceError):
            b.image_name = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


class TestExecute:
    """Requirement: Stub activity types — Execute."""

    def test_kind_is_execute(self) -> None:
        e = Execute(command="python train.py")
        assert e.kind == "execute"

    def test_to_dict(self) -> None:
        e = Execute(command="python train.py")
        assert e.to_dict() == {"command": "python train.py", "kind": "execute"}

    def test_frozen(self) -> None:
        e = Execute()
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.command = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class TestPlan:
    """Requirement: Plan as ordered list of activities."""

    def test_to_dict_preserves_order(self) -> None:
        plan = Plan(
            activities=[
                BuildImage(image_name="img:1", dockerfile="Dockerfile"),
                Execute(command="python train.py"),
            ]
        )
        result = plan.to_dict()
        assert len(result) == 2
        assert result[0]["kind"] == "build_image"
        assert result[1]["kind"] == "execute"

    def test_to_json_is_valid_json(self) -> None:
        plan = Plan(
            activities=[
                BuildImage(image_name="img:1", dockerfile="Dockerfile"),
                Execute(command="python train.py"),
            ]
        )
        parsed = json.loads(plan.to_json())
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0]["kind"] == "build_image"

    def test_empty_plan(self) -> None:
        plan = Plan(activities=[])
        assert plan.to_dict() == []
        assert json.loads(plan.to_json()) == []
