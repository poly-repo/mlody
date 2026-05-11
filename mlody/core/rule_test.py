"""Tests for rule.mlody — _COMMON_ATTRS, _validate_name, conflict detection, merge.

All scenarios trace back to the rule-common-attrs spec:
  openspec/changes/rule-common-attrs/specs/rule-common-attrs/spec.md
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS

_THIS_DIR = Path(__file__).parent
_RULE_MLODY = (_THIS_DIR / "rule.mlody").read_text()
_ATTRS_MLODY = (_THIS_DIR.parent / "common" / "attrs.mlody").read_text()

# Minimal base file set: only rule.mlody and attrs.mlody are needed for these
# unit tests. We test rule() in isolation using a trivial implementation.
_BASE_FILES: dict[str, str] = {
    "mlody/core/rule.mlody": _RULE_MLODY,
    "mlody/common/attrs.mlody": _ATTRS_MLODY,
}

# A minimal .mlody preamble that loads only rule and attr.
_PREAMBLE = (
    'load("//mlody/core/rule.mlody", "rule")\n'
    'load("//mlody/common/attrs.mlody", "attr")\n'
)


def _eval_script(script: str) -> Evaluator:
    """Evaluate a .mlody script string using the base files and return the Evaluator."""
    files = dict(_BASE_FILES)
    files["test.mlody"] = _PREAMBLE + dedent(script)
    with InMemoryFS(files, root="/project") as root:
        ev = Evaluator(root)
        ev.eval_file(root / "test.mlody")
        ev.resolve()
    return ev


def _eval_raises(script: str) -> Exception:
    """Assert that evaluating the script raises an exception and return it."""
    with pytest.raises(Exception) as exc_info:
        _eval_script(script)
    return exc_info.value


# ---------------------------------------------------------------------------
# Task 3.1 — _validate_name scenarios
# ---------------------------------------------------------------------------


class TestValidateNameEmptyString:
    """Scenario: Empty name raises ValueError."""

    def test_empty_name_raises_value_error(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="")
        """)
        assert "must not be empty" in str(exc)


class TestValidateNameWithSpace:
    """Scenario: Name with space raises ValueError."""

    def test_name_with_space_raises_and_includes_value(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="my task")
        """)
        assert "my task" in str(exc)


class TestValidateNameWithSlash:
    """Scenario: Name with slash raises ValueError."""

    def test_name_with_slash_raises(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="my/task")
        """)
        assert isinstance(exc, ValueError)


class TestValidateNameValidAlphanumericUnderscore:
    """Scenario: Valid name with letters, digits, and underscore succeeds."""

    def test_valid_name_does_not_raise(self) -> None:
        # Should evaluate without error
        _eval_script("""\
            def _impl(ctx):
                return {}
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="valid_name_123")
        """)

    def test_ctx_attr_name_matches(self) -> None:
        # Verify ctx.attr.name is accessible and equals the supplied name.
        # We use builtins.register with a supported kind to surface the value.
        ev = _eval_script("""\
            def _impl(ctx):
                builtins.register("representation", struct(name=ctx.attr.name))
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="valid_name_123")
        """)
        result = ev.registry.all.get(("representation", "test", "valid_name_123"))
        assert result is not None
        assert result.name == "valid_name_123"


class TestValidateNameSingleChar:
    """Scenario: Single alphabetic character is a valid name."""

    def test_single_letter_name_succeeds(self) -> None:
        _eval_script("""\
            def _impl(ctx):
                return {}
            my_rule = rule(implementation=_impl, kind="representation")
            my_rule(name="A")
        """)


# ---------------------------------------------------------------------------
# Task 3.2 — Conflict detection scenarios
# ---------------------------------------------------------------------------


class TestConflictOnNameKey:
    """Scenario: Conflict on name key raises at definition time."""

    def test_name_key_conflict_raises_value_error(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            rule(implementation=_impl, kind="representation", attrs={"name": attr(type="string")})
        """)
        assert isinstance(exc, ValueError)
        assert "name" in str(exc)


class TestConflictOnDescriptionKey:
    """Scenario: Conflict on description key raises at definition time."""

    def test_description_key_conflict_raises_value_error(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            rule(implementation=_impl, kind="representation", attrs={"description": attr(type="string", mandatory=False)})
        """)
        assert isinstance(exc, ValueError)
        assert "description" in str(exc)


class TestConflictOnMethodsKey:
    """Scenario: Conflict on methods key raises at definition time."""

    def test_methods_key_conflict_raises_value_error(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            rule(implementation=_impl, kind="representation", attrs={"methods": attr(type="dict", mandatory=False)})
        """)
        assert isinstance(exc, ValueError)
        assert "methods" in str(exc)


class TestConflictOnBothKeys:
    """Scenario: Conflict on multiple keys names all conflicting keys."""

    def test_both_keys_conflicting_names_both_in_error(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            rule(
                implementation=_impl,
                kind="representation",
                attrs={
                    "name": attr(type="string"),
                    "description": attr(type="string", mandatory=False),
                },
            )
        """)
        assert isinstance(exc, ValueError)
        assert "name" in str(exc)
        assert "description" in str(exc)


class TestNoConflict:
    """Scenario: Non-conflicting attrs do not trigger an error."""

    def test_non_conflicting_attrs_no_error(self) -> None:
        # "outputs" is not in _COMMON_ATTRS — should load cleanly.
        _eval_script("""\
            def _impl(ctx):
                return {}
            rule(
                implementation=_impl,
                kind="representation",
                attrs={"extra_field": attr(mandatory=False)},
            )
        """)


# ---------------------------------------------------------------------------
# Task 3.3 — Merge behavior scenarios
# ---------------------------------------------------------------------------


class TestMergeAttrsIsNone:
    """Scenario: attrs=None treated as empty dict — no TypeError."""

    def test_attrs_none_does_not_raise(self) -> None:
        _eval_script("""\
            def _impl(ctx):
                return {}
            r = rule(implementation=_impl, kind="representation", attrs=None)
            r(name="ok")
        """)


class TestMergeAttrsEmptyDict:
    """Scenario: attrs={} results in name and description present."""

    def test_name_and_description_available_with_empty_attrs(self) -> None:
        # description defaults to "" so accessing ctx.attr.description must work.
        # We use the "representation" kind (simplest supported kind for testing).
        _eval_script("""\
            def _impl(ctx):
                builtins.register("representation", struct(
                    name=ctx.attr.name,
                    description=ctx.attr.description,
                ))
            r = rule(implementation=_impl, kind="representation", attrs={})
            r(name="myname")
        """)

    def test_description_defaults_to_empty_string(self) -> None:
        ev = _eval_script("""\
            def _impl(ctx):
                builtins.register("representation", struct(
                    name=ctx.attr.name,
                    description=ctx.attr.description,
                ))
            r = rule(implementation=_impl, kind="representation", attrs={})
            r(name="myname")
        """)
        # Evaluator stores representations under key (kind, stem, name).
        # _stem is the relative path of the file that registered it, sans suffix.
        thing = ev.registry.all.get(("representation", "test", "myname"))
        assert thing is not None
        assert thing.name == "myname"
        assert thing.description == ""


class TestMergeNonConflictingAttrs:
    """Scenario: Non-conflicting attrs merged correctly alongside _COMMON_ATTRS."""

    def test_extra_attr_present_after_merge(self) -> None:
        ev = _eval_script("""\
            def _impl(ctx):
                builtins.register("representation", struct(
                    name=ctx.attr.name,
                    description=ctx.attr.description,
                    extra=ctx.attr.extra,
                ))
            r = rule(
                implementation=_impl,
                kind="representation",
                attrs={"extra": attr(type="string")},
            )
            r(name="mything", extra="hello")
        """)
        thing = ev.registry.all.get(("representation", "test", "mything"))
        assert thing is not None
        assert thing.name == "mything"
        assert thing.extra == "hello"
        assert thing.description == ""


class TestMethodsDefaultsAndValidation:
    """Scenarios covering the shared methods field."""

    def test_methods_defaults_to_none(self) -> None:
        ev = _eval_script("""\
            def _impl(ctx):
                builtins.register("representation", struct(
                    name=ctx.attr.name,
                    methods=ctx.attr.methods,
                ))
            r = rule(implementation=_impl, kind="representation", attrs={})
            r(name="mything")
        """)
        thing = ev.registry.all.get(("representation", "test", "mything"))
        assert thing is not None
        assert thing.methods is None

    def test_methods_rejects_non_callable_entries(self) -> None:
        exc = _eval_raises("""\
            def _impl(ctx):
                return {}
            r = rule(implementation=_impl, kind="representation", attrs={})
            r(name="bad", methods={"info": 1})
        """)
        assert isinstance(exc, TypeError)
        assert "methods[info]" in str(exc)
