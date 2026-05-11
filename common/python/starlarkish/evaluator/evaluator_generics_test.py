"""Evaluator-level tests for generic registration, method registry, and isolation.

These tests verify:
- generic kind registration and lookup via _register / _lookup
- register_method arity enforcement
- get_methods returning an empty list for unknown names
- per-instance registry isolation (two Evaluator instances, same process)
- config kind registration and lookup
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.registry import SUPPORTED_REGISTRATION_KINDS
from common.python.starlarkish.evaluator.testing import InMemoryFS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_eval(script: str) -> Evaluator:
    """Run script inside an InMemoryFS evaluator and return the Evaluator."""
    files = {"test.mlody": dedent(script)}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
    return ev


# ---------------------------------------------------------------------------
# 1. Generic registration and lookup
# ---------------------------------------------------------------------------


def test_generic_registered_and_retrievable_by_name() -> None:
    """builtins.register('generic', ...) stores the struct; _lookup finds it.

    Ref: Scenario 'Register a generic struct' from starlarkish spec.
    """
    ev = _minimal_eval("""\
        builtins.register("generic", struct(kind="generic", name="render"))
    """)
    assert "render" in ev._generics_by_name
    g = ev._generics_by_name["render"]
    assert getattr(g, "kind") == "generic"
    assert getattr(g, "name") == "render"


def test_generic_lookup_via_builtin_lookup() -> None:
    """builtins.lookup('generic', 'render') returns the registered struct.

    Ref: Scenario 'Lookup a registered generic by name' from starlarkish spec.
    """
    ev = _minimal_eval("""\
        builtins.register("generic", struct(kind="generic", name="render"))
        result = builtins.lookup("generic", "render")
        builtins.register("root", struct(name="r", data=result))
    """)
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert getattr(data, "kind") == "generic"
    assert getattr(data, "name") == "render"


def test_generic_lookup_nonexistent_raises() -> None:
    """Lookup of a non-registered generic raises NameError.

    Ref: Scenario 'Lookup a non-existent generic raises an error'.
    """
    files = {"test.mlody": "builtins.lookup('generic', 'nonexistent')"}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(NameError, match="nonexistent"):
            ev.eval_file(root / "test.mlody")


def test_register_unknown_kind_raises() -> None:
    """Registering an unknown kind raises a descriptive ValueError."""
    files = {
        "test.mlody": "builtins.register('unicorn', struct(kind='unicorn', name='x'))"
    }
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(ValueError, match="unicorn"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 2. register_method arity enforcement
# ---------------------------------------------------------------------------


def _make_method_struct_script(generic_name: str, patterns_repr: str) -> str:
    """Return a Starlark snippet that registers a method via builtins.register_method."""
    return dedent(f"""\
        def _body(ctx, *args):
            pass

        _method = struct(
            kind="method",
            generic="{generic_name}",
            patterns={patterns_repr},
            body=_body,
        )
        builtins.register_method("{generic_name}", _method)
    """)


def test_register_method_sets_arity_on_first_call() -> None:
    """First register_method call fixes the arity for the generic.

    Ref: Scenario 'First method sets arity'.
    """
    script = _make_method_struct_script("render", '["train"]')
    files = {"test.mlody": script}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    entry = ev._method_registry.get("render")
    assert entry is not None
    assert entry["arity"] == 1
    assert len(entry["methods"]) == 1


def test_register_method_consistent_arity_accepted() -> None:
    """Consistent arity accepted for subsequent method calls.

    Ref: Scenario 'Consistent arity accepted'.
    """
    script = (
        _make_method_struct_script("render", '["train"]')
        + _make_method_struct_script("render", '["serve"]')
    )
    files = {"test.mlody": script}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    assert ev._method_registry["render"]["arity"] == 1
    assert len(ev._method_registry["render"]["methods"]) == 2


def test_register_method_mixed_arity_raises() -> None:
    """Mixed arity raises ValueError with a message naming the generic and arities.

    Ref: Scenario 'Mixed arity raises ValueError'.
    """
    script = (
        _make_method_struct_script("render", '["train"]')
        + _make_method_struct_script("render", '["train", "gpu"]')
    )
    files = {"test.mlody": script}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        with pytest.raises(ValueError, match="render"):
            ev.eval_file(root / "test.mlody")


# ---------------------------------------------------------------------------
# 3. get_methods
# ---------------------------------------------------------------------------


def test_get_methods_returns_empty_list_for_unknown_name() -> None:
    """get_methods for a never-registered generic returns an empty list.

    Ref: Scenario 'get_methods returns empty list for unknown generic'.
    """
    ev = _minimal_eval("""\
        result = builtins.get_methods("neverregistered")
        builtins.register("root", struct(name="r", data=result))
    """)
    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert list(data) == []


def test_get_methods_returns_registered_methods() -> None:
    """get_methods returns the list of registered method structs.

    Ref: Scenario 'get_methods returns current method list'.
    """
    script = (
        _make_method_struct_script("render", '["train"]')
        + dedent("""\
            result = builtins.get_methods("render")
            builtins.register("root", struct(name="r", data=result))
        """)
    )
    files = {"test.mlody": script}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    data = ev._roots_by_name["r"].data  # type: ignore[attr-defined]
    assert len(list(data)) == 1


# ---------------------------------------------------------------------------
# 4. Per-instance registry isolation
# ---------------------------------------------------------------------------


def test_per_instance_registry_isolation() -> None:
    """Methods registered on one Evaluator do NOT appear in another.

    Ref: Scenario 'Registry is per-evaluator-instance'.
    """
    script = _make_method_struct_script("render", '["train"]')
    files = {"test.mlody": script}

    with InMemoryFS(files, root="/project") as root:
        ev1 = Evaluator(root)
        ev1.eval_file(root / "test.mlody")

    with InMemoryFS(files, root="/project") as root:
        ev2 = Evaluator(root)
        ev2.eval_file(root / "test.mlody")

    # Both have the method, but in distinct dicts
    assert ev1._method_registry is not ev2._method_registry
    assert "render" in ev1._method_registry
    assert "render" in ev2._method_registry


def test_registry_clear_resets_methods() -> None:
    """Clearing _method_registry removes all previously registered methods.

    Ref: Scenario 'Test isolation via clear'.
    """
    script = _make_method_struct_script("render", '["train"]')
    files = {"test.mlody": script}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    assert "render" in ev._method_registry
    ev._method_registry.clear()
    assert "render" not in ev._method_registry


# ---------------------------------------------------------------------------
# 5. Config kind registration (tasks 7.1, 7.2, 7.3)
# ---------------------------------------------------------------------------


def test_config_kind_in_supported_registration_kinds() -> None:
    """'config' is present in SUPPORTED_REGISTRATION_KINDS.

    Ref: Scenario '"config" is in SUPPORTED_REGISTRATION_KINDS'.
    """
    assert "config" in SUPPORTED_REGISTRATION_KINDS


def test_registry_state_for_kind_config_returns_configs_bucket() -> None:
    """RegistryState.for_kind('config') returns the configs NamedRegistry.

    Ref: Scenario 'for_kind lookup succeeds'.
    """
    files = {"test.mlody": ""}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    bucket = ev.registry.for_kind("config", operation="lookup")
    assert bucket is ev.registry.configs


def test_config_registration_roundtrip_via_builtins() -> None:
    """builtins.register('config', struct(...)) stores in registry.configs.by_name.

    Ref: Scenario 'config registration does not raise ValueError' and
    Scenario '"config" registration round-trip'.
    """
    ev = _minimal_eval("""\
        builtins.register("config", struct(name="team", kind="config", description="d", rules={}))
    """)
    assert "team" in ev.registry.configs.by_name
    cfg = ev.registry.configs.by_name["team"]
    assert getattr(cfg, "name") == "team"


# ---------------------------------------------------------------------------
# 6. User kind registration
# ---------------------------------------------------------------------------


def test_user_kind_in_supported_registration_kinds() -> None:
    """'user' is present in SUPPORTED_REGISTRATION_KINDS."""
    assert "user" in SUPPORTED_REGISTRATION_KINDS


def test_registry_state_for_kind_user_returns_users_bucket() -> None:
    """RegistryState.for_kind('user') returns the users NamedRegistry."""
    files = {"test.mlody": ""}
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")

    bucket = ev.registry.for_kind("user", operation="lookup")
    assert bucket is ev.registry.users


def test_user_registration_roundtrip_via_builtins() -> None:
    """builtins.register('user', struct(...)) stores in registry.users.by_name."""
    ev = _minimal_eval("""\
        builtins.register("user", struct(
            kind="user",
            name="agarcia",
            description="Ava Garcia",
            groups=["framera", "framera-admin"],
        ))
    """)
    assert "agarcia" in ev.registry.users.by_name
    user = ev.registry.users.by_name["agarcia"]
    assert getattr(user, "name") == "agarcia"
    assert getattr(user, "description") == "Ava Garcia"
