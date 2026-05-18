"""Constructor tests for registered-entity dataclasses."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS
import mlody
from mlody.common.action import RegisteredAction
from mlody.common.build_ref import RegisteredBuildRef
from mlody.common.execution import RegisteredExecution
from mlody.common.freshness import RegisteredFreshness
from mlody.common.generic import RegisteredGeneric
from mlody.common.implementation import RegisteredImplementation
from mlody.common.location import RegisteredLocation
from mlody.common.representation import RegisteredRepresentation
from mlody.common.root import RegisteredRoot
from mlody.common.struct import Struct
from mlody.common.task import RegisteredTask
from mlody.common.type import RegisteredType
from mlody.common.user import RegisteredUser
from mlody.common.value import RegisteredValue

assert mlody.__name__ == "mlody"

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR.parent / "core" / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR / "attrs.mlody").read_text()
_TYPES_MLODY = (_THIS_DIR / "types.mlody").read_text()
_FRESHNESS_MLODY = (_THIS_DIR / "freshness.mlody").read_text()
_LOCATIONS_MLODY = (_THIS_DIR / "locations.mlody").read_text()
_REPRESENTATION_MLODY = (_THIS_DIR / "representation.mlody").read_text()
_BUILD_REF_MLODY = (_THIS_DIR / "build_ref.mlody").read_text()
_IMPLEMENTATION_MLODY = (_THIS_DIR / "implementation.mlody").read_text()
_EXECUTION_MLODY = (_THIS_DIR / "execution.mlody").read_text()
_VALUES_MLODY = (_THIS_DIR / "values.mlody").read_text()
_ACTION_MLODY = (_THIS_DIR / "action.mlody").read_text()
_TASK_MLODY = (_THIS_DIR / "task.mlody").read_text()
_USER_MLODY = (_THIS_DIR / "user.mlody").read_text()
_MM_MLODY = (_THIS_DIR / "mm.mlody").read_text()

_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
    "mlody/common/types.mlody": _TYPES_MLODY,
    "mlody/common/freshness.mlody": _FRESHNESS_MLODY,
    "mlody/common/locations.mlody": _LOCATIONS_MLODY,
    "mlody/common/representation.mlody": _REPRESENTATION_MLODY,
    "mlody/common/build_ref.mlody": _BUILD_REF_MLODY,
    "mlody/common/implementation.mlody": _IMPLEMENTATION_MLODY,
    "mlody/common/execution.mlody": _EXECUTION_MLODY,
    "mlody/common/values.mlody": _VALUES_MLODY,
    "mlody/common/action.mlody": _ACTION_MLODY,
    "mlody/common/task.mlody": _TASK_MLODY,
    "mlody/common/user.mlody": _USER_MLODY,
    "mlody/common/mm.mlody": _MM_MLODY,
}

_PREAMBLE = dedent(
    """\
    load("//mlody/common/types.mlody")
    load("//mlody/common/freshness.mlody")
    load("//mlody/common/locations.mlody")
    load("//mlody/common/representation.mlody")
    load("//mlody/common/build_ref.mlody")
    load("//mlody/common/implementation.mlody")
    load("//mlody/common/execution.mlody")
    load("//mlody/common/values.mlody")
    load("//mlody/common/action.mlody")
    load("//mlody/common/task.mlody")
    load("//mlody/common/user.mlody")
    load("//mlody/common/mm.mlody", "mm")

    builtins.register("root", struct(name="test_root", path="//mlody/common", description="shared"))
    mm.generic("render", description="Render generic")
    value(name="inp", description="Task input", type=integer(), location=s3())
    action(
        name="act",
        description="Run the sample action",
        inputs=["inp"],
        outputs=[],
        implementation=shell_script(content="echo hello"),
    )
    task(name="tsk", description="Execute the sample task", inputs=["inp"], outputs=[], action="act")
    user(name="agarcia", description="Ava Garcia", groups=["framera", "framera-admin"])
    """
)


def _sample_registry_structs() -> dict[str, Struct]:
    files = dict(_BASE_FILES)
    files["test.mlody"] = _PREAMBLE
    with InMemoryFS(files, root="/project") as root:
        evaluator = Evaluator(root)
        evaluator.eval_file(root / "test.mlody")
        evaluator.resolve()
        registry = evaluator.registry
        return {
            "root": registry.roots.by_name["test_root"],
            "type": registry.types.by_name["integer"],
            "location": registry.locations.by_name["s3"],
            "freshness": registry.freshnesses.by_name["always"],
            "representation": registry.representations.by_name["text"],
            "value": registry.values.by_name["inp"],
            "action": registry.actions.by_name["act"],
            "task": registry.tasks.by_name["tsk"],
            "user": registry.users.by_name["agarcia"],
            "implementation": registry.implementations.by_name["shell_script"],
            "build_ref": registry.build_refs.by_name["bazel"],
            "execution": registry.executions.by_name["localhost"],
            "generic": registry.generics.by_name["render"],
        }


@pytest.mark.parametrize(
    ("wrapper_type", "sample_key"),
    [
        (RegisteredRoot, "root"),
        (RegisteredType, "type"),
        (RegisteredLocation, "location"),
        (RegisteredFreshness, "freshness"),
        (RegisteredRepresentation, "representation"),
        (RegisteredValue, "value"),
        (RegisteredAction, "action"),
        (RegisteredTask, "task"),
        (RegisteredUser, "user"),
        (RegisteredImplementation, "implementation"),
        (RegisteredBuildRef, "build_ref"),
        (RegisteredExecution, "execution"),
        (RegisteredGeneric, "generic"),
    ],
    ids=[
        "root",
        "type",
        "location",
        "freshness",
        "representation",
        "value",
        "action",
        "task",
        "user",
        "implementation",
        "build_ref",
        "execution",
        "generic",
    ],
)
def test_registered_wrapper_accepts_evaluator_struct(
    wrapper_type: type[object],
    sample_key: str,
) -> None:
    sample = _sample_registry_structs()[sample_key]
    wrapped = wrapper_type(sample)  # type: ignore[call-arg]
    assert wrapped is not None
    assert getattr(wrapped, "kind") == sample_key


def test_registered_action_rejects_unknown_fields() -> None:
    action = _sample_registry_structs()["action"]
    invalid = Struct(**action.as_mapping(), unexpected_attr="boom")
    with pytest.raises(ValueError, match="unexpected_attr"):
        RegisteredAction(invalid)


def test_registered_action_normalizes_port_lists_to_named_value_map() -> None:
    action = RegisteredAction(_sample_registry_structs()["action"])
    assert action.description == "Run the sample action"
    assert list(action.inputs) == ["inp"]
    assert isinstance(action.inputs["inp"], RegisteredValue)
    assert action.inputs["inp"].name == "inp"
    assert action.inputs["inp"].description == "Task input"


def test_registered_task_accepts_workspace_named_port_structs() -> None:
    task_struct = _sample_registry_structs()["task"]
    inputs = Struct(inp=_sample_registry_structs()["value"])
    task_struct = task_struct.updated(
        inputs=inputs,
        outputs=Struct(),
        config=Struct(),
    )
    task = RegisteredTask(task_struct)
    assert task.description == "Execute the sample task"
    assert list(task.inputs) == ["inp"]
    assert isinstance(task.inputs["inp"], RegisteredValue)
    assert task.inputs["inp"].name == "inp"
    assert task.inputs["inp"].description == "Task input"


# ---------------------------------------------------------------------------
# F1b — __init_subclass__ registry  (task 3.12)
# ---------------------------------------------------------------------------


class TestRegisteredStructBaseRegistry:
    """F1b: RegisteredStructBase._REGISTERED is auto-populated via __init_subclass__."""

    def test_registered_contains_exactly_14_entries(self) -> None:
        """F1b: _REGISTERED contains exactly 14 entries after import.

        Scenario: _REGISTERED is populated after module import.
        OQ-05: RegisteredGeneric (kind='generic') is included, giving 14 total.
        """
        from mlody.common._registered_struct import RegisteredStructBase

        # Import all concrete subclasses to ensure __init_subclass__ has fired
        import mlody.common.action  # noqa: F401
        import mlody.common.build_ref  # noqa: F401
        import mlody.common.config  # noqa: F401
        import mlody.common.execution  # noqa: F401
        import mlody.common.freshness  # noqa: F401
        import mlody.common.generic  # noqa: F401
        import mlody.common.implementation  # noqa: F401
        import mlody.common.location  # noqa: F401
        import mlody.common.representation  # noqa: F401
        import mlody.common.root  # noqa: F401
        import mlody.common.task  # noqa: F401
        import mlody.common.type  # noqa: F401
        import mlody.common.user  # noqa: F401
        import mlody.common.value  # noqa: F401

        assert len(RegisteredStructBase._REGISTERED) == 14

    def test_registered_keys_match_expected_kinds(self) -> None:
        """F1b: _REGISTERED contains exactly the expected 14 kind strings.

        This assertion guards against R-001 (test-only subclasses polluting
        the registry) — only concrete classes with a non-empty string _KIND register.
        """
        from mlody.common._registered_struct import RegisteredStructBase

        expected_kinds = {
            "root",
            "type",
            "location",
            "freshness",
            "representation",
            "value",
            "action",
            "task",
            "user",
            "build_ref",
            "implementation",
            "execution",
            "config",
            "generic",
        }
        assert set(RegisteredStructBase._REGISTERED.keys()) == expected_kinds

    def test_wrap_registered_struct_returns_registered_action_for_action_kind(self) -> None:
        """F1b: wrap_registered_struct('action', struct) returns RegisteredAction.

        Scenario: wrap_registered_struct returns the correct type.
        """
        from mlody.common._registered_struct import wrap_registered_struct

        action_struct = _sample_registry_structs()["action"]
        result = wrap_registered_struct("action", action_struct)
        assert isinstance(result, RegisteredAction)

    def test_wrap_registered_struct_returns_same_type_as_before_refactor(self) -> None:
        """F1b: The type returned by wrap_registered_struct is unchanged.

        Scenario: wrap_registered_struct returns the correct type.
        Tests the 5 kinds present in _sample_registry_structs() that are in
        _ENTITY_KINDS_FOR_STRUCT_WRAP (no config struct is defined in the preamble).
        """
        from mlody.common._registered_struct import wrap_registered_struct

        for kind in ("root", "value", "action", "task", "user"):
            result = wrap_registered_struct(kind, _sample_registry_structs()[kind])
            assert result is not None
            assert getattr(result, "kind") == kind

    def test_wrap_method_result_returns_registered_value_for_value_struct(self) -> None:
        """F1b: wrap_method_result(value_struct) returns RegisteredValue.

        Scenario: wrap_method_result returns the correct type.
        """
        from mlody.common._registered_struct import wrap_method_result
        from mlody.common.value import RegisteredValue as _RV

        value_struct = _sample_registry_structs()["value"]
        result = wrap_method_result(value_struct)
        assert isinstance(result, _RV)

    def test_wrapper_for_kind_does_not_exist_after_refactor(self) -> None:
        """F1b: _wrapper_for_kind is NOT an attribute of the module after refactor.

        Scenario: _wrapper_for_kind does not exist after refactor.
        """
        import mlody.common._registered_struct as _rs

        assert not hasattr(_rs, "_wrapper_for_kind")

    def test_method_wrapper_for_kind_does_not_exist_after_refactor(self) -> None:
        """F1b: _method_wrapper_for_kind is NOT an attribute of the module after refactor.

        Scenario: _method_wrapper_for_kind does not exist after refactor.
        """
        import mlody.common._registered_struct as _rs

        assert not hasattr(_rs, "_method_wrapper_for_kind")

    def test_abstract_subclass_without_kind_does_not_pollute_registry(self) -> None:
        """F1b: R-001 mitigation — classes without _KIND are not registered.

        A test-only subclass that does not declare _KIND must not appear in
        _REGISTERED.
        """
        from mlody.common._registered_struct import RegisteredStructBase

        class _TestSubclassWithoutKind(RegisteredStructBase):
            pass

        # The new subclass must not appear in _REGISTERED
        assert "_TestSubclassWithoutKind" not in RegisteredStructBase._REGISTERED
        assert None not in RegisteredStructBase._REGISTERED
        # Count must remain 14 after this subclass is defined
        assert len(RegisteredStructBase._REGISTERED) == 14
