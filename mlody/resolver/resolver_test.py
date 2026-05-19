"""Tests for mlody.resolver.resolver — label parsing, SHA resolution, and factory."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.core.workspace import Workspace, WorkspaceStateKind
from mlody.resolver.errors import (
    AmbiguousRefError,
    BranchTagCollisionError,
    LabelParseError,
    LockBusyError,
    UnknownRefError,
    WorkspaceResolutionError,
)
from mlody.resolver.resolver import (
    Reporter,
    WorkspaceRequest,
    _make_workspace_request,
    evict_baseline_workspace,
    evict_cwd_baseline_workspaces,
    ResolvedRef,
    configure_workspace,
    get_or_build_baseline_workspace,
    parse_label,
    reload_baseline_workspace,
    resolve_sha,
    resolve_workspace,
)

SHA_MAIN = "a" * 40
SHA_TAG = "b" * 40
SHA_ALT = "c" * 40


# ---------------------------------------------------------------------------
# parse_label
# ---------------------------------------------------------------------------


class TestParseLabel:
    """Requirement: Extended label syntax parsing."""

    def test_at_prefix_passthrough(self) -> None:
        # Scenario: cwd label passthrough — @-prefixed
        committoid, inner = parse_label("@lexica//models:bert")
        assert committoid is None
        assert inner == "@lexica//models:bert"

    def test_double_slash_prefix_passthrough(self) -> None:
        # Scenario: cwd label passthrough — //-prefixed
        committoid, inner = parse_label("//models:bert")
        assert committoid is None
        assert inner == "//models:bert"

    def test_branch_name_committoid(self) -> None:
        # Scenario: committoid-qualified label — branch name
        committoid, inner = parse_label("main|@lexica//models:bert")
        assert committoid == "main"
        assert inner == "@lexica//models:bert"

    def test_tag_name_committoid(self) -> None:
        # Scenario: committoid-qualified label — tag name
        committoid, inner = parse_label("v1.2.0|//models:bert")
        assert committoid == "v1.2.0"
        assert inner == "//models:bert"

    def test_short_sha_committoid(self) -> None:
        # Scenario: committoid-qualified label — short SHA
        committoid, inner = parse_label("abc1234|@lexica//models:bert")
        assert committoid == "abc1234"
        assert inner == "@lexica//models:bert"

    def test_attribute_path_is_preserved_on_entity_label(self) -> None:
        # Scenario: entity label with tick attribute path keeps tick separator in inner label.
        committoid, inner = parse_label("@common//huggingface/downloader:downloader'outputs.model")
        assert committoid is None
        assert inner == "@common//huggingface/downloader:downloader'outputs.model"

    def test_attribute_path_is_preserved_with_committoid(self) -> None:
        # Scenario: committoid + entity tick attribute path preserves tick separator.
        committoid, inner = parse_label("main|@common//huggingface/downloader:downloader'outputs.model")
        assert committoid == "main"
        assert inner == "@common//huggingface/downloader:downloader'outputs.model"

    def test_entity_field_path_is_preserved_in_inner_label(self) -> None:
        # Scenario: entity field path (dot after colon) is preserved using dot separator.
        committoid, inner = parse_label("@common//huggingface/downloader:downloader.outputs.model")
        assert committoid is None
        assert inner == "@common//huggingface/downloader:downloader.outputs.model"

    def test_query_only_wildcard_entity_query_is_preserved(self) -> None:
        committoid, inner = parse_label('//...:[@mlody _.kind == "task"]')
        assert committoid is None
        assert inner == '//...:[@mlody _.kind == "task"]'

    def test_bare_root_label_round_trips(self) -> None:
        # Scenario: bare @root with no path re-serialises without // suffix.
        committoid, inner = parse_label("@lexica")
        assert committoid is None
        assert inner == "@lexica"

    def test_bare_workspace_label_returns_empty_inner(self) -> None:
        # Scenario: bare committoid label (no entity, no attribute) is a valid
        # workspace reference — returns (committoid, "") for bare-workspace resolution.
        committoid, inner = parse_label("notaref")
        assert committoid == "notaref"
        assert inner == ""

    def test_inner_label_not_at_or_slash_raises_label_parse_error(self) -> None:
        # Scenario: invalid inner label (entity does not start with @//) raises LabelParseError
        with pytest.raises(LabelParseError) as exc_info:
            parse_label("main|notaninnerref")
        assert exc_info.value.label == "main|notaninnerref"
        assert "//" in exc_info.value.reason


# ---------------------------------------------------------------------------
# resolve_sha
# ---------------------------------------------------------------------------


def _make_git_client(pairs: list[tuple[str, str]]) -> MagicMock:
    client = MagicMock()
    client.ls_remote.return_value = pairs
    client.local_remote_tracking_refs.return_value = []
    client.rev_parse_local.return_value = None
    return client


class TestResolveSha:
    """Requirement: Committoid resolution via git ls-remote."""

    def test_branch_resolves_to_sha(self) -> None:
        # Scenario: branch name resolves to full SHA
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/main"),
            (SHA_TAG, "refs/heads/other"),
        ])
        result = resolve_sha("main", client)
        assert result.sha == SHA_MAIN
        assert not result.local_only

    def test_lightweight_tag_resolves(self) -> None:
        # Scenario: exact tag name resolves — lightweight tag
        client = _make_git_client([
            (SHA_TAG, "refs/tags/v1.0.0"),
        ])
        result = resolve_sha("v1.0.0", client)
        assert result.sha == SHA_TAG
        assert not result.local_only

    def test_annotated_tag_prefers_deref_sha(self) -> None:
        # Scenario: annotated tag prefers ^{} entry
        tag_obj_sha = "a" * 40
        commit_sha = "b" * 40
        client = _make_git_client([
            (tag_obj_sha, "refs/tags/v1.0.0"),
            (commit_sha, "refs/tags/v1.0.0^{}"),
        ])
        result = resolve_sha("v1.0.0", client)
        assert result.sha == commit_sha
        assert not result.local_only

    def test_short_sha_resolves_unique_prefix(self) -> None:
        # Scenario: short SHA resolves when exactly one remote SHA matches prefix
        full_sha = "abc1234" + "0" * 33
        other_sha = "def5678" + "0" * 33
        client = _make_git_client([
            (full_sha, "refs/heads/main"),
            (other_sha, "refs/heads/feature"),
        ])
        result = resolve_sha("abc1234", client)
        assert result.sha == full_sha
        assert not result.local_only

    def test_unknown_ref_raises(self) -> None:
        # Scenario: unknown ref raises UnknownRefError
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/main"),
        ])
        with pytest.raises(UnknownRefError) as exc_info:
            resolve_sha("nosuchbranch", client)
        assert exc_info.value.committoid == "nosuchbranch"
        assert exc_info.value.remote == "origin"

    def test_ambiguous_short_sha_raises(self) -> None:
        # Scenario: ambiguous short SHA raises AmbiguousRefError
        sha1 = "abc1234" + "0" * 33
        sha2 = "abc1234" + "1" * 33
        client = _make_git_client([
            (sha1, "refs/heads/main"),
            (sha2, "refs/heads/feature"),
        ])
        with pytest.raises(AmbiguousRefError) as exc_info:
            resolve_sha("abc1234", client)
        assert exc_info.value.committoid == "abc1234"
        assert sha1 in exc_info.value.matching_shas
        assert sha2 in exc_info.value.matching_shas

    def test_branch_tag_collision_raises(self) -> None:
        # Scenario: branch and tag share the same name raises BranchTagCollisionError
        client = _make_git_client([
            (SHA_MAIN, "refs/heads/v1.0"),
            (SHA_TAG, "refs/tags/v1.0"),
        ])
        with pytest.raises(BranchTagCollisionError) as exc_info:
            resolve_sha("v1.0", client)
        assert exc_info.value.name == "v1.0"


class TestResolveShаLocalFallback:
    """Requirement: Local-only refs resolve and are flagged as not landed."""

    def test_local_only_partial_sha_resolves(self) -> None:
        # Scenario: partial SHA not on remote but present locally → local_only=True
        local_sha = "d" * 40
        client = _make_git_client([])
        client.rev_parse_local.return_value = local_sha

        result = resolve_sha("dddddddd", client)

        assert result == ResolvedRef(sha=local_sha, local_only=True)

    def test_local_only_branch_resolves(self) -> None:
        # Scenario: branch exists only locally → local_only=True
        local_sha = "e" * 40
        client = _make_git_client([])
        client.rev_parse_local.return_value = local_sha

        result = resolve_sha("my-local-branch", client)

        assert result == ResolvedRef(sha=local_sha, local_only=True)

    def test_remote_ref_not_marked_local_only(self) -> None:
        # Scenario: ref found on remote → local_only=False even if locally present
        client = _make_git_client([(SHA_MAIN, "refs/heads/main")])
        client.rev_parse_local.return_value = SHA_MAIN

        result = resolve_sha("main", client)

        assert result.sha == SHA_MAIN
        assert not result.local_only
        client.rev_parse_local.assert_not_called()

    def test_all_sources_empty_raises(self) -> None:
        # Scenario: no remote, no tracking refs, no local match → UnknownRefError
        client = _make_git_client([])

        with pytest.raises(UnknownRefError):
            resolve_sha("completely-unknown", client)


# ---------------------------------------------------------------------------
# resolve_workspace — factory
# ---------------------------------------------------------------------------


class TestConfigureWorkspace:
    """Requirement: CLI config overrides are applied through workspace-aware setf."""

    def test_loaded_workspace_is_promoted_to_baseline_then_forked(self) -> None:
        workspace = object.__new__(Workspace)
        workspace._state_kind = WorkspaceStateKind.LOADED
        workspace.mark_baseline = MagicMock(
            side_effect=lambda: (
                setattr(workspace, "_state_kind", WorkspaceStateKind.BASELINE) or workspace
            )
        )
        request_workspace = MagicMock(spec=Workspace)
        workspace.fork_request = MagicMock(return_value=request_workspace)

        with (
            patch("mlody.resolver.resolver._normalize_workspace_defaults") as mock_normalize,
            patch("mlody.resolver.resolver._normalize_action_implementations") as mock_impls,
            patch("mlody.resolver.resolver._apply_registered_configs") as mock_configs,
            patch(
                "mlody.resolver.resolver.apply_request_overrides",
                return_value=request_workspace,
            ) as mock_apply_request,
        ):
            result = configure_workspace(workspace, ["//simple:flag=true"])

        assert result is request_workspace
        mock_normalize.assert_called_once_with(workspace)
        mock_impls.assert_called_once_with(workspace)
        mock_configs.assert_called_once_with(workspace)
        workspace.mark_baseline.assert_called_once_with()
        workspace.fork_request.assert_called_once_with()
        mock_apply_request.assert_called_once_with(
            request_workspace,
            ["//simple:flag=true"],
        )
        assert workspace.state_kind is WorkspaceStateKind.BASELINE

    def test_baseline_workspace_is_forked_without_reapplying_defaults(self) -> None:
        baseline = object.__new__(Workspace)
        baseline._state_kind = WorkspaceStateKind.BASELINE
        request_workspace = MagicMock(spec=Workspace)
        baseline.fork_request = MagicMock(return_value=request_workspace)

        with (
            patch("mlody.resolver.resolver._normalize_workspace_defaults") as mock_normalize,
            patch("mlody.resolver.resolver._apply_registered_configs") as mock_configs,
            patch(
                "mlody.resolver.resolver.apply_request_overrides",
                return_value=request_workspace,
            ) as mock_apply_request,
        ):
            result = configure_workspace(baseline, [])

        assert result is request_workspace
        mock_normalize.assert_not_called()
        mock_configs.assert_not_called()
        baseline.fork_request.assert_called_once_with()
        mock_apply_request.assert_called_once_with(request_workspace, [])

    def test_request_workspace_applies_overrides_in_place(self) -> None:
        request_workspace = object.__new__(Workspace)
        request_workspace._state_kind = WorkspaceStateKind.REQUEST
        request_workspace.fork_request = MagicMock()

        with patch(
            "mlody.resolver.resolver.apply_request_overrides",
            return_value=request_workspace,
        ) as mock_apply_request:
            result = configure_workspace(request_workspace, ["//simple:flag=true"])

        assert result is request_workspace
        request_workspace.fork_request.assert_not_called()
        mock_apply_request.assert_called_once_with(
            request_workspace,
            ["//simple:flag=true"],
        )

    def test_repeated_calls_on_same_baseline_return_isolated_request_workspaces(self) -> None:
        workspace = object.__new__(Workspace)
        workspace._state_kind = WorkspaceStateKind.BASELINE
        request_a = MagicMock(spec=Workspace)
        request_b = MagicMock(spec=Workspace)
        workspace.fork_request = MagicMock(side_effect=[request_a, request_b])

        def _record_config(request_workspace: Workspace, config: list[str]) -> Workspace:
            request_workspace.applied_config = tuple(config)
            return request_workspace

        with patch(
            "mlody.resolver.resolver.apply_request_overrides",
            side_effect=_record_config,
        ) as mock_apply_request:
            result_a = configure_workspace(workspace, ["//simple:first=true"])
            result_b = configure_workspace(workspace, ["//simple:second=true"])

        assert result_a is request_a
        assert result_b is request_b
        assert request_a.applied_config == ("//simple:first=true",)
        assert request_b.applied_config == ("//simple:second=true",)
        assert not hasattr(workspace, "applied_config")
        assert workspace.fork_request.call_count == 2
        mock_apply_request.assert_any_call(request_a, ["//simple:first=true"])
        mock_apply_request.assert_any_call(request_b, ["//simple:second=true"])

    def test_inline_value_target_updates_inline_location_payload(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = [
            "@lexica//services/release/api/image:cfg",
        ]
        location = Struct(
            kind="location",
            type="inline",
            name="inline",
            attributes={},
            _allowed_attrs={},
        )
        workspace.resolve.return_value = Struct(
            kind="value",
            name="cfg",
            type=Struct(kind="type", type="string", name="string"),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            result = configure_workspace(
                workspace,
                ["@lexica//services/release/api/image:cfg=abc123"],
            )

        assert result is workspace
        mock_setf.assert_called_once()
        call = mock_setf.call_args
        assert call.args[0] == "@lexica//services/release/api/image:cfg.location"
        updated_location = call.args[1]
        assert updated_location.data == "abc123"
        assert updated_location.attributes == {}
        assert call.kwargs["workspace"] is workspace
        assert (
            call.kwargs["source"]
            == "COMMAND_LINE: @lexica//services/release/api/image:cfg=abc123"
        )

    def test_non_inline_target_uses_direct_setf_assignment(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = [
            "@lexica//services/release/api/image:image.config.commit_sha",
        ]
        workspace.resolve.return_value = "old"

        with patch("mlody.core.setf.setf") as mock_setf:
            result = configure_workspace(
                workspace,
                ["@lexica//services/release/api/image:image.config.commit_sha=new-api"],
            )

        assert result is workspace
        mock_setf.assert_called_once_with(
            "@lexica//services/release/api/image:image.config.commit_sha",
            "new-api",
            workspace=workspace,
            source=(
                "COMMAND_LINE: "
                "@lexica//services/release/api/image:image.config.commit_sha=new-api"
            ),
        )

    def test_mlody_query_target_splits_on_top_level_equals(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = [
            '@lexica//pipeline:deploy.sha[@mlody _.kind == "action"]',
        ]
        workspace.resolve.return_value = "old"

        with patch("mlody.core.setf.setf") as mock_setf:
            result = configure_workspace(
                workspace,
                ['//...:[@mlody _.kind == "action"].sha=foo'],
            )

        assert result is workspace
        mock_setf.assert_called_once_with(
            '@lexica//pipeline:deploy.sha[@mlody _.kind == "action"]',
            "foo",
            workspace=workspace,
            source='COMMAND_LINE: //...:[@mlody _.kind == "action"].sha=foo',
        )

    def test_mlody_query_target_falls_back_to_setf_for_missing_field(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = [
            '@lexica//pipeline:deploy.config.sha[@mlody _.kind == "action"]',
        ]
        workspace.resolve.side_effect = AttributeError("sha")

        with patch("mlody.core.setf.setf") as mock_setf:
            result = configure_workspace(
                workspace,
                ['//...:[@mlody _.kind == "action"].config.sha=foo'],
            )

        assert result is workspace
        mock_setf.assert_called_once_with(
            '@lexica//pipeline:deploy.config.sha[@mlody _.kind == "action"]',
            "foo",
            workspace=workspace,
            source='COMMAND_LINE: //...:[@mlody _.kind == "action"].config.sha=foo',
        )

    def test_value_may_contain_additional_equals(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = [
            "@lexica//services/release/api/image:image.config.commit_sha",
        ]
        workspace.resolve.return_value = "old"

        with patch("mlody.core.setf.setf") as mock_setf:
            result = configure_workspace(
                workspace,
                ["@lexica//services/release/api/image:image.config.commit_sha=foo=bar"],
            )

        assert result is workspace
        mock_setf.assert_called_once_with(
            "@lexica//services/release/api/image:image.config.commit_sha",
            "foo=bar",
            workspace=workspace,
            source=(
                "COMMAND_LINE: "
                "@lexica//services/release/api/image:image.config.commit_sha=foo=bar"
            ),
        )

    def test_wildcard_override_with_no_matches_raises(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = []

        with pytest.raises(WorkspaceResolutionError, match="matched no entities"):
            configure_workspace(
                workspace,
                ['//...:[@mlody _.kind == "action"].sha=foo'],
            )

    def test_invalid_config_entry_raises_workspace_resolution_error(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()

        with pytest.raises(WorkspaceResolutionError, match="LABEL=VALUE"):
            configure_workspace(workspace, ["@lexica//services/release/api/image:image"])

        workspace.resolve.assert_not_called()

    def test_inline_value_with_failing_validator_raises(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:commit"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        workspace.resolve.return_value = Struct(
            kind="value",
            name="commit",
            type=Struct(
                kind="type",
                type="commit",
                name="commit",
                validator=lambda v: (_ for _ in ()).throw(TypeError(f"bad sha: {v}")),
            ),
            location=location,
            _lineage=[],
        )

        with pytest.raises(WorkspaceResolutionError, match="not valid for type"):
            configure_workspace(workspace, ["//simple:commit=xxx"])

    def test_inline_value_canonical_is_applied(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:commit"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        full_sha = "a" * 40
        workspace.resolve.return_value = Struct(
            kind="value",
            name="commit",
            type=Struct(
                kind="type",
                type="commit",
                name="commit",
                validator=lambda v: True,
                canonical=lambda v: full_sha,
            ),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            configure_workspace(workspace, ["//simple:commit=abc123"])

        updated_location = mock_setf.call_args.args[1]
        assert updated_location.data == full_sha

    def test_bool_string_coerced_to_true_in_data(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:b"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        _TRUTHY = {"true", "yes", "1"}
        _FALSY = {"false", "no", "0"}

        def _coerce_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                if v.lower() in _TRUTHY:
                    return True
                if v.lower() in _FALSY:
                    return False
                raise TypeError(f"Expected bool-like string, got {v!r}")
            raise TypeError(f"Expected bool-like value, got {type(v)!r}")

        workspace.resolve.return_value = Struct(
            kind="value",
            name="b",
            type=Struct(
                kind="type",
                type="bool",
                name="bool",
                canonical=_coerce_bool,
                validator=lambda v: None if isinstance(v, bool) else (_ for _ in ()).throw(TypeError(f"Expected bool, got {type(v)}")),
            ),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            configure_workspace(workspace, ["//simple:b=yes"])

        updated_location = mock_setf.call_args.args[1]
        assert updated_location.data is True
        assert mock_setf.call_args.kwargs["source"] == "COMMAND_LINE: //simple:b=True"

    def test_integer_string_is_coerced(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:i"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        workspace.resolve.return_value = Struct(
            kind="value",
            name="i",
            type=Struct(
                kind="type",
                type="integer",
                name="integer",
                validator=lambda v: None if (isinstance(v, int) and not isinstance(v, bool)) else (_ for _ in ()).throw(TypeError(f"Expected int, got {type(v)}")),
                canonical=lambda v: int(v) if isinstance(v, str) else v,
            ),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            configure_workspace(workspace, ["//simple:i=5"])

        updated_location = mock_setf.call_args.args[1]
        assert updated_location.data == 5

    def test_integer_invalid_string_raises(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:i"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        workspace.resolve.return_value = Struct(
            kind="value",
            name="i",
            type=Struct(
                kind="type",
                type="integer",
                name="integer",
                canonical=lambda v: int(v),
            ),
            location=location,
            _lineage=[],
        )

        with pytest.raises(WorkspaceResolutionError, match="not valid for type 'integer'"):
            configure_workspace(workspace, ["//simple:i=abc"])

    def test_float_string_is_coerced(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:f"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        workspace.resolve.return_value = Struct(
            kind="value",
            name="f",
            type=Struct(
                kind="type",
                type="float",
                name="float",
                canonical=lambda v: float(v) if isinstance(v, str) else v,
            ),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            configure_workspace(workspace, ["//simple:f=3.14"])

        updated_location = mock_setf.call_args.args[1]
        assert updated_location.data == pytest.approx(3.14)

    def test_bool_invalid_string_raises(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:b"]
        location = Struct(kind="location", type="inline", name="inline", attributes={}, _allowed_attrs={})
        _TRUTHY = {"true", "yes", "1"}
        _FALSY = {"false", "no", "0"}

        def _strict_coerce_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                if v.lower() in _TRUTHY:
                    return True
                if v.lower() in _FALSY:
                    return False
                raise TypeError(f"Expected bool-like string, got {v!r}")
            raise TypeError(f"Expected bool-like value, got {type(v)!r}")

        workspace.resolve.return_value = Struct(
            kind="value",
            name="b",
            type=Struct(
                kind="type",
                type="bool",
                name="bool",
                canonical=_strict_coerce_bool,
            ),
            location=location,
            _lineage=[],
        )

        with pytest.raises(WorkspaceResolutionError, match="not valid for type 'bool'"):
            configure_workspace(workspace, ["//simple:b=maybe"])

    def test_with_quantity_string_converted_to_declared_unit(self) -> None:
        from astropy import units as u

        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:d"]
        location = Struct(
            kind="location",
            type="inline",
            name="inline",
            attributes={},
            _allowed_attrs={},
        )
        workspace.resolve.return_value = Struct(
            kind="value",
            name="d",
            unit=u.Unit("m/s"),
            type=Struct(
                kind="type",
                type="float",
                name="float",
                canonical=lambda v: float(v) if isinstance(v, str) else v,
                validator=lambda v: None,
            ),
            location=location,
            _lineage=[],
        )

        with patch("mlody.core.setf.setf") as mock_setf:
            configure_workspace(workspace, ["//simple:d=3600m/h"])

        updated_location = mock_setf.call_args.args[1]
        assert updated_location.data == pytest.approx(1.0)

    def test_with_quantity_string_incompatible_unit_raises(self) -> None:
        from astropy import units as u

        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
        workspace.expand_wildcard_label.return_value = ["//simple:d"]
        location = Struct(
            kind="location",
            type="inline",
            name="inline",
            attributes={},
            _allowed_attrs={},
        )
        workspace.resolve.return_value = Struct(
            kind="value",
            name="d",
            unit=u.Unit("m/s"),
            type=Struct(
                kind="type",
                type="float",
                name="float",
                canonical=lambda v: float(v) if isinstance(v, str) else v,
                validator=lambda v: None,
            ),
            location=location,
            _lineage=[],
        )

        with pytest.raises(
            WorkspaceResolutionError, match="cannot parse as quantity"
        ):
            configure_workspace(workspace, ["//simple:d=3kg"])


def _make_cwd_request(monorepo_root: Path) -> WorkspaceRequest:
    """Build a minimal cwd WorkspaceRequest for test use."""
    return _make_workspace_request(mode="cwd", monorepo_root=monorepo_root)


_NOOP_REPORTER = Reporter(print_fn=lambda *a, **kw: None)


class TestWorkspaceRequest:
    """Requirement: WorkspaceRequest is a frozen dataclass that is its own cache key."""

    def test_workspace_request_is_hashable(self, tmp_path: Path) -> None:
        # Scenario: WorkspaceRequest is hashable and usable as a dict key
        req = _make_cwd_request(tmp_path)
        d: dict[WorkspaceRequest, str] = {req: "value"}
        assert d[req] == "value"

    def test_cache_key_returns_self(self, tmp_path: Path) -> None:
        # Scenario: cache_key() returns self
        req = _make_cwd_request(tmp_path)
        assert req.cache_key() is req

    def test_identical_requests_compare_equal(self, tmp_path: Path) -> None:
        # Scenario: two identical WorkspaceRequests compare equal
        fn = print
        req1 = WorkspaceRequest(
            mode="cwd",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            roots_file=tmp_path / "mlody" / "roots.mlody",
            full_workspace=False,
            extra_roots=(),
            lazy_roots=(),
            print_fn=fn,
            console=None,
            resolved_sha=None,
        )
        req2 = WorkspaceRequest(
            mode="cwd",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            roots_file=tmp_path / "mlody" / "roots.mlody",
            full_workspace=False,
            extra_roots=(),
            lazy_roots=(),
            print_fn=fn,
            console=None,
            resolved_sha=None,
        )
        assert req1 == req2

    def test_baseline_workspace_cache_key_not_importable(self) -> None:
        # Scenario: BaselineWorkspaceCacheKey is not importable after refactor
        import mlody.resolver.resolver as resolver_mod  # noqa: PLC0415
        assert not hasattr(resolver_mod, "BaselineWorkspaceCacheKey")


class TestBaselineWorkspaceCache:
    def test_workspace_request_distinguishes_different_inputs(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "sandbox"
        cwd_req = _make_workspace_request(mode="cwd", monorepo_root=tmp_path)
        workspace_req = _make_workspace_request(
            mode="cwd",
            monorepo_root=tmp_path,
            workspace_root=workspace_root,
            extra_roots={"workspace": "sandbox"},
            lazy_roots={"mlody": "mlody"},
        )
        commit_req = _make_workspace_request(
            mode="commit",
            monorepo_root=tmp_path / "cache" / SHA_MAIN,
            resolved_sha=SHA_MAIN,
        )

        assert isinstance(cwd_req, WorkspaceRequest)
        assert len({cwd_req, workspace_req, commit_req}) == 3

    def test_get_or_build_returns_cached_baseline_on_repeat(
        self,
        tmp_path: Path,
    ) -> None:
        raw_workspace = MagicMock()
        baseline_workspace = MagicMock(spec=Workspace)
        req = _make_cwd_request(tmp_path)
        evict_baseline_workspace(req)

        with (
            patch("mlody.resolver.resolver.Workspace", return_value=raw_workspace) as mock_ws_cls,
            patch(
                "mlody.resolver.resolver.build_baseline_workspace",
                return_value=baseline_workspace,
            ) as mock_build,
        ):
            first = get_or_build_baseline_workspace(req, _NOOP_REPORTER)
            second = get_or_build_baseline_workspace(req, _NOOP_REPORTER)

        assert first is baseline_workspace
        assert second is baseline_workspace
        mock_ws_cls.assert_called_once()
        raw_workspace.load.assert_called_once()
        mock_build.assert_called_once_with(raw_workspace)
        evict_baseline_workspace(req)

    def test_reload_baseline_workspace_rebuilds_cached_entry(
        self,
        tmp_path: Path,
    ) -> None:
        raw_workspace_a = MagicMock()
        raw_workspace_b = MagicMock()
        baseline_a = MagicMock(spec=Workspace)
        baseline_b = MagicMock(spec=Workspace)
        req = _make_cwd_request(tmp_path)
        evict_baseline_workspace(req)

        with (
            patch(
                "mlody.resolver.resolver.Workspace",
                side_effect=[raw_workspace_a, raw_workspace_b],
            ) as mock_ws_cls,
            patch(
                "mlody.resolver.resolver.build_baseline_workspace",
                side_effect=[baseline_a, baseline_b],
            ) as mock_build,
        ):
            first = get_or_build_baseline_workspace(req, _NOOP_REPORTER)
            second = reload_baseline_workspace(req, _NOOP_REPORTER)

        assert first is baseline_a
        assert second is baseline_b
        assert mock_ws_cls.call_count == 2
        assert raw_workspace_a.load.call_count == 1
        assert raw_workspace_b.load.call_count == 1
        assert mock_build.call_count == 2
        evict_baseline_workspace(req)

    def test_evict_cwd_baselines_keeps_commit_entries(
        self,
        tmp_path: Path,
    ) -> None:
        commit_root = tmp_path / "cache" / SHA_MAIN
        raw_cwd_a = MagicMock()
        raw_commit = MagicMock()
        raw_cwd_b = MagicMock()
        baseline_cwd_a = MagicMock(spec=Workspace)
        baseline_commit = MagicMock(spec=Workspace)
        baseline_cwd_b = MagicMock(spec=Workspace)
        cwd_req = _make_cwd_request(tmp_path)
        commit_req = _make_workspace_request(
            mode="commit",
            monorepo_root=commit_root,
            resolved_sha=SHA_MAIN,
        )
        evict_baseline_workspace(cwd_req)
        evict_baseline_workspace(commit_req)

        with (
            patch(
                "mlody.resolver.resolver.Workspace",
                side_effect=[raw_cwd_a, raw_commit, raw_cwd_b],
            ) as mock_ws_cls,
            patch(
                "mlody.resolver.resolver.build_baseline_workspace",
                side_effect=[baseline_cwd_a, baseline_commit, baseline_cwd_b],
            ),
        ):
            get_or_build_baseline_workspace(cwd_req, _NOOP_REPORTER)
            get_or_build_baseline_workspace(commit_req, _NOOP_REPORTER)

            removed = evict_cwd_baseline_workspaces(monorepo_root=tmp_path)

            cwd_after = get_or_build_baseline_workspace(cwd_req, _NOOP_REPORTER)
            commit_after = get_or_build_baseline_workspace(commit_req, _NOOP_REPORTER)

        assert removed == 1
        assert cwd_after is baseline_cwd_b
        assert commit_after is baseline_commit
        assert mock_ws_cls.call_count == 3
        evict_baseline_workspace(cwd_req)
        evict_baseline_workspace(commit_req)


class TestResolveWorkspaceCwdPath:
    """Requirement: resolve_workspace cwd passthrough."""

    def test_cwd_label_returns_monorepo_workspace_and_none_sha(
        self, tmp_path: Path
    ) -> None:
        # Scenario: cwd path — label starts with @
        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            mock_ws = MagicMock()
            request_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws
            mock_configure.return_value = request_ws

            ws, sha = resolve_workspace("@lexica//models:bert", monorepo_root=tmp_path)

        assert sha is None
        assert ws is request_ws
        mock_ws_cls.assert_called_once_with(
            monorepo_root=tmp_path,
            roots_file=tmp_path / "mlody" / "roots.mlody",
            full_workspace=False,
            print_fn=print,
            console=None,
            extra_roots=None,
            lazy_roots=None,
            workspace_root=None,
        )
        mock_ws.load.assert_called_once()
        mock_configure.assert_called_once_with(mock_ws, [])

    def test_double_slash_label_returns_cwd_workspace(self, tmp_path: Path) -> None:
        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            mock_ws = MagicMock()
            request_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws
            mock_configure.return_value = request_ws

            ws, sha = resolve_workspace("//models:bert", monorepo_root=tmp_path)

        assert sha is None
        assert ws is request_ws

    def test_config_overrides_are_applied_before_return(self, tmp_path: Path) -> None:
        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws
            mock_configure.return_value = mock_ws

            ws, sha = resolve_workspace(
                "@lexica//models:bert",
                monorepo_root=tmp_path,
                config=["@lexica//models:bert.config.token=abc123"],
            )

        assert sha is None
        assert ws is mock_ws
        mock_configure.assert_called_once_with(
            mock_ws,
            ["@lexica//models:bert.config.token=abc123"],
        )

    def test_repeated_cwd_resolves_reuse_cached_baseline(self, tmp_path: Path) -> None:
        raw_workspace = MagicMock()
        request_workspace_a = MagicMock()
        request_workspace_b = MagicMock()

        with (
            patch(
                "mlody.resolver.resolver.Workspace",
                return_value=raw_workspace,
            ) as mock_ws_cls,
            patch(
                "mlody.resolver.resolver.configure_workspace",
                side_effect=[request_workspace_a, request_workspace_b],
            ) as mock_configure,
        ):
            first_workspace, first_sha = resolve_workspace(
                "@lexica//models:bert",
                monorepo_root=tmp_path,
                config=["@lexica//models:bert.config.token=first"],
            )
            second_workspace, second_sha = resolve_workspace(
                "@lexica//models:bert",
                monorepo_root=tmp_path,
                config=["@lexica//models:bert.config.token=second"],
            )

        assert first_sha is None
        assert second_sha is None
        assert first_workspace is request_workspace_a
        assert second_workspace is request_workspace_b
        mock_ws_cls.assert_called_once()
        raw_workspace.load.assert_called_once()
        mock_configure.assert_any_call(
            raw_workspace,
            ["@lexica//models:bert.config.token=first"],
        )
        mock_configure.assert_any_call(
            raw_workspace,
            ["@lexica//models:bert.config.token=second"],
        )

    def test_user_description_is_canonicalized_before_configuration(
        self, tmp_path: Path
    ) -> None:
        baseline = object.__new__(Workspace)
        baseline._evaluator = MagicMock()
        baseline._state_kind = WorkspaceStateKind.BASELINE
        baseline.evaluator.registry.users.by_name = {
            "mav": Struct(
                kind="user",
                name="mav",
                description="Maurizio Vitale",
                groups=["admin"],
            )
        }
        request_workspace = MagicMock()
        baseline.fork_request = MagicMock(return_value=request_workspace)

        with (
            patch(
                "mlody.resolver.resolver.resolve_workspace_baseline",
                return_value=(baseline, None),
            ) as mock_baseline,
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            mock_configure.return_value = request_workspace
            workspace, sha = resolve_workspace(
                "@lexica//models:bert",
                monorepo_root=tmp_path,
                user="Maurizio Vitale",
                config=["@lexica//models:bert.config.token=abc123"],
            )

        assert sha is None
        assert workspace is request_workspace
        mock_baseline.assert_called_once()
        baseline.fork_request.assert_called_once_with()
        request_workspace.update_global_context.assert_called_once_with(
            user="mav",
            resolved_sha=None,
        )
        mock_configure.assert_called_once_with(
            request_workspace,
            ["@lexica//models:bert.config.token=abc123"],
        )

    def test_invalid_user_raises_before_configuration(self, tmp_path: Path) -> None:
        baseline = MagicMock()
        baseline.evaluator.registry.users.by_name = {
            "mav": Struct(
                kind="user",
                name="mav",
                description="Maurizio Vitale",
                groups=["admin"],
            )
        }

        with (
            patch(
                "mlody.resolver.resolver.resolve_workspace_baseline",
                return_value=(baseline, None),
            ),
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            with pytest.raises(
                WorkspaceResolutionError,
                match="Valid users: mav \\(Maurizio Vitale\\)",
            ):
                resolve_workspace(
                    "@lexica//models:bert",
                    monorepo_root=tmp_path,
                    user="nobody",
                )

        baseline.fork_request.assert_not_called()
        mock_configure.assert_not_called()


class TestResolveWorkspaceCommittoidPath:
    """Requirement: resolve_workspace committoid path — branch cache miss."""

    def _make_fake_client(self, full_sha: str) -> MagicMock:
        client = MagicMock()
        client.ls_remote.return_value = [(full_sha, "refs/heads/main")]
        client.cat_file_type.return_value = "commit"
        client.remote_url.return_value = "git@github.com:org/repo.git"
        return client

    def test_returns_workspace_and_sha_on_cache_miss(self, tmp_path: Path) -> None:
        # Scenario: branch name, cache miss → materialise → workspace returned
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch("mlody.resolver.resolver.configure_workspace") as mock_configure,
        ):
            mock_ws = MagicMock()
            request_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws
            mock_configure.return_value = request_ws

            ws, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha
        assert ws is request_ws
        # Workspace constructed from the cache dir
        dest = cache_root / full_sha
        mock_ws_cls.assert_called_once_with(
            monorepo_root=dest,
            roots_file=dest / "mlody" / "roots.mlody",
            full_workspace=False,
            print_fn=print,
            console=None,
            extra_roots=None,
            lazy_roots=None,
            workspace_root=None,
        )
        mock_ws.load.assert_called_once()
        mock_configure.assert_called_once_with(mock_ws, [])

    def test_cache_hit_skips_clone(self, tmp_path: Path) -> None:
        # Scenario: committoid path — cache hit skips cloning
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        # Pre-create the sentinel so check_cache returns "hit"
        sentinel = cache_root / full_sha / "mlody" / "roots.mlody"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()

        client = self._make_fake_client(full_sha)

        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha
        # Clone methods must NOT be called on cache hit
        client.clone_local.assert_not_called()
        client.clone_remote.assert_not_called()

    def test_repeated_commit_resolves_reuse_cached_baseline(self, tmp_path: Path) -> None:
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)
        raw_workspace = MagicMock()
        request_workspace_a = MagicMock()
        request_workspace_b = MagicMock()

        with (
            patch("mlody.resolver.resolver.materialise", return_value=cache_root / full_sha),
            patch(
                "mlody.resolver.resolver.Workspace",
                return_value=raw_workspace,
            ) as mock_ws_cls,
            patch(
                "mlody.resolver.resolver.configure_workspace",
                side_effect=[request_workspace_a, request_workspace_b],
            ) as mock_configure,
        ):
            first_workspace, first_sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
                config=["@lexica//models:bert.config.token=first"],
            )
            second_workspace, second_sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
                config=["@lexica//models:bert.config.token=second"],
            )

        assert first_sha == full_sha
        assert second_sha == full_sha
        assert first_workspace is request_workspace_a
        assert second_workspace is request_workspace_b
        mock_ws_cls.assert_called_once()
        raw_workspace.load.assert_called_once()
        mock_configure.assert_any_call(
            raw_workspace,
            ["@lexica//models:bert.config.token=first"],
        )
        mock_configure.assert_any_call(
            raw_workspace,
            ["@lexica//models:bert.config.token=second"],
        )

    def test_resolver_exceptions_propagate_unchanged(self, tmp_path: Path) -> None:
        # Scenario: all resolver exceptions propagate to caller
        cache_root = tmp_path / "cache"
        client = MagicMock()
        client.ls_remote.return_value = []  # nothing → UnknownRefError
        client.local_remote_tracking_refs.return_value = []
        client.rev_parse_local.return_value = None

        with pytest.raises(UnknownRefError):
            resolve_workspace(
                "nosuchbranch|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

    def test_resolved_sha_is_none_for_cwd_path(self, tmp_path: Path) -> None:
        # Scenario: resolved_sha=None on cwd path
        # Use a label with a non-empty path — the core parser requires path to
        # be non-empty after '//' (grammar: path_segments = 1*path_component).
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            _, sha = resolve_workspace("@bert//models:lr", monorepo_root=tmp_path)

        assert sha is None

    def test_resolved_sha_is_full_sha_on_committoid_path(self, tmp_path: Path) -> None:
        # Scenario: resolved_sha=<sha> on committoid path
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value = MagicMock()
            _, sha = resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
            )

        assert sha == full_sha


# ---------------------------------------------------------------------------
# resolve_workspace — value_description / DB integration (task 5.3)
# ---------------------------------------------------------------------------


class TestResolveWorkspaceValueDescription:
    """Requirement: value_description wires DB write after materialisation."""

    def _make_fake_client(self, full_sha: str) -> MagicMock:
        client = MagicMock()
        client.ls_remote.return_value = [(full_sha, "refs/heads/main")]
        client.cat_file_type.return_value = "commit"
        client.remote_url.return_value = "git@github.com:org/repo.git"
        return client

    def test_value_description_writes_db_row(self, tmp_path: Path) -> None:
        # Scenario: value_description provided → one DB row is written.
        # We redirect the DB write to tmp_path by injecting a custom db_path
        # via patching _record_evaluation_best_effort's internal Path.home call.
        cache_root = tmp_path / "cache"
        db_path = tmp_path / "mlody.sqlite"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        # Capture the write by routing open_db to our tmp_path db
        import mlody.resolver.resolver as resolver_mod
        from mlody.db.evaluations import open_db

        original_fn = resolver_mod._record_evaluation_best_effort
        captured_calls: list[dict[str, object]] = []

        def fake_record(**kwargs: object) -> None:
            captured_calls.append(kwargs)
            conn = open_db(db_path)
            try:
                from mlody.db.evaluations import write_evaluation

                write_evaluation(
                    conn,
                    username="testuser",
                    hostname="testhost",
                    requested_ref=str(kwargs["committoid"]),
                    resolved_sha=str(kwargs["resolved_sha"]),
                    resolved_at=str(kwargs["resolved_at"]),
                    repo=str(kwargs["repo_url"]),
                    local_only=bool(kwargs["local_only"]),
                    value_description=str(kwargs["value_description"]),
                )
            finally:
                conn.close()

        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch.object(resolver_mod, "_record_evaluation_best_effort", fake_record),
        ):
            mock_ws_cls.return_value = MagicMock()
            resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
                value_description="bert-base-uncased config",
            )

        assert len(captured_calls) == 1
        assert captured_calls[0]["resolved_sha"] == full_sha
        assert captured_calls[0]["value_description"] == "bert-base-uncased config"

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()
            assert count is not None
            assert count[0] == 1
            row = conn.execute(
                "SELECT resolved_sha, value_description FROM evaluations"
            ).fetchone()
            assert row is not None
            assert row[0] == full_sha
            assert row[1] == "bert-base-uncased config"
        finally:
            conn.close()

    def test_no_value_description_skips_db_write(self, tmp_path: Path) -> None:
        # Scenario: value_description omitted → _record_evaluation_best_effort not called
        cache_root = tmp_path / "cache"
        full_sha = SHA_MAIN
        client = self._make_fake_client(full_sha)

        import mlody.resolver.resolver as resolver_mod

        called: list[bool] = []

        def should_not_be_called(**kwargs: object) -> None:
            called.append(True)

        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch.object(resolver_mod, "_record_evaluation_best_effort", should_not_be_called),
        ):
            mock_ws_cls.return_value = MagicMock()
            resolve_workspace(
                "main|@lexica//models:bert",
                monorepo_root=tmp_path,
                git_client=client,
                cache_root=cache_root,
                # value_description not provided
            )

        assert called == []

    def test_cwd_label_with_value_description_skips_db_write(self, tmp_path: Path) -> None:
        # Scenario: cwd-relative label — no committoid → DB write is skipped
        # even if value_description is provided (no resolved_sha to record)
        import mlody.resolver.resolver as resolver_mod

        called: list[bool] = []

        def should_not_be_called(**kwargs: object) -> None:
            called.append(True)

        with (
            patch("mlody.resolver.resolver.Workspace") as mock_ws_cls,
            patch.object(resolver_mod, "_record_evaluation_best_effort", should_not_be_called),
        ):
            mock_ws_cls.return_value = MagicMock()
            _, sha = resolve_workspace(
                "@lexica//models:bert",
                monorepo_root=tmp_path,
                value_description="should not write",
            )

        assert sha is None
        assert called == []


# ---------------------------------------------------------------------------
# _apply_registered_configs tests (tasks 10.2, 10.3)
# ---------------------------------------------------------------------------


def test_apply_registered_configs_rules_applied_via_setf() -> None:
    """_apply_registered_configs applies rules via setf with correct source string.

    Ref: Scenario 'Phase 4 — Config application'.
    """
    from mlody.resolver.resolver import _apply_registered_configs  # noqa: PLC0415

    config_struct = Struct(
        kind="config",
        name="defaults",
        description="",
        rules={":lr": 0.001, ":epochs": 10},
    )
    fake_workspace = MagicMock()
    fake_workspace.registry_view.configs_snapshot.return_value = [
        ("mlody/teams/lexica/config:defaults", config_struct)
    ]

    setf_calls: list[tuple[str, object, str]] = []

    def fake_setf(label: str, value: object, *, workspace: object, source: str) -> None:
        setf_calls.append((label, value, source))

    with patch("mlody.core.setf.setf", side_effect=fake_setf):
        import mlody.core.setf  # noqa: PLC0415
        _apply_registered_configs(fake_workspace)

    assert len(setf_calls) == 2
    labels = {label for label, _v, _s in setf_calls}
    # :lr and :epochs are in the same file as the config (mlody/teams/lexica/config)
    assert labels == {"//mlody/teams/lexica/config:lr", "//mlody/teams/lexica/config:epochs"}
    for label, value, source in setf_calls:
        assert source == f"CONFIG: defaults: {label}={value}"


def test_apply_registered_configs_hierarchical_order() -> None:
    """Configs at shallower paths apply before deeper-path configs; deeper wins.

    Ref: Scenario 'Config application respects hierarchical order'.
    """
    from mlody.resolver.resolver import _apply_registered_configs  # noqa: PLC0415

    shallow_config = Struct(
        kind="config",
        name="root_defaults",
        description="",
        rules={":lr": 0.01},
    )
    deep_config = Struct(
        kind="config",
        name="team_defaults",
        description="",
        rules={":lr": 0.001},
    )
    fake_workspace = MagicMock()
    fake_workspace.registry_view.configs_snapshot.return_value = [
        ("mlody/config:root_defaults", shallow_config),
        ("mlody/teams/pixella/config:team_defaults", deep_config),
    ]

    applied_order: list[tuple[str, object]] = []

    def fake_setf(label: str, value: object, *, workspace: object, source: str) -> None:
        applied_order.append((label, value))

    with patch("mlody.core.setf.setf", side_effect=fake_setf):
        import mlody.core.setf  # noqa: PLC0415
        _apply_registered_configs(fake_workspace)

    assert len(applied_order) == 2
    # :lr resolves to the same file stem as each config; shallower path applies first
    assert applied_order[0] == ("//mlody/config:lr", 0.01)
    assert applied_order[1] == ("//mlody/teams/pixella/config:lr", 0.001)


def test_apply_registered_configs_defaults_task_execution_without_overwriting_explicit_values() -> None:
    """Task execution config rules only fill tasks that do not already set execution."""
    from mlody.resolver.resolver import _apply_registered_configs  # noqa: PLC0415

    default_execution = Struct(
        kind="execution",
        name="localhost",
        type="localhost",
    )
    config_struct = Struct(
        kind="config",
        name="workspace_defaults",
        description="",
        rules={'//...:[@mlody _.kind == "task"].execution': default_execution},
    )
    fake_workspace = MagicMock()
    fake_workspace.registry_view.configs_snapshot.return_value = [
        ("workspace:workspace_defaults", config_struct)
    ]
    fake_workspace.expand_wildcard_label.return_value = [
        '@lexica//pipeline:missing.execution[@mlody _.kind == "task"]',
        '@lexica//pipeline:explicit.execution[@mlody _.kind == "task"]',
    ]
    resolved_by_label = {
        '@lexica//pipeline:missing[@mlody _.kind == "task"]': Struct(
            kind="task",
            name="missing",
            execution=None,
        ),
        '@lexica//pipeline:explicit[@mlody _.kind == "task"]': Struct(
            kind="task",
            name="explicit",
            execution=Struct(kind="execution", name="docker", type="docker"),
        ),
    }
    fake_workspace.resolve.side_effect = lambda label: resolved_by_label[label]

    setf_calls: list[tuple[str, object, str]] = []

    def fake_setf(label: str, value: object, *, workspace: object, source: str) -> None:
        setf_calls.append((label, value, source))

    with patch("mlody.core.setf.setf", side_effect=fake_setf):
        import mlody.core.setf  # noqa: PLC0415
        _apply_registered_configs(fake_workspace)

    assert setf_calls == [
        (
            '@lexica//pipeline:missing.execution[@mlody _.kind == "task"]',
            default_execution,
            'CONFIG: workspace_defaults: //...:[@mlody _.kind == "task"].execution='
            + str(default_execution),
        )
    ]


def test_normalize_action_implementations_defaults_build_backed_actions() -> None:
    """Missing implementations are synthesized as sandbox(build=...) defaults."""
    from mlody.resolver.resolver import _normalize_action_implementations  # noqa: PLC0415

    build_ref = Struct(
        kind="build_ref",
        name="bazel",
        type="bazel",
        target=":model-download",
    )
    action_struct = Struct(
        kind="action",
        name="downloader-action",
        implementation=None,
        build=build_ref,
    )
    task_struct = Struct(
        kind="task",
        name="downloader",
        action=action_struct,
    )
    fake_workspace = MagicMock()
    fake_workspace.root_infos = {}
    fake_workspace._workspace_root = Path("/repo")
    fake_workspace._monorepo_root = Path("/repo")
    fake_workspace.registry_view.iter_registry_items.return_value = [
        (("action", "mlody/common/huggingface/downloader", "downloader-action"), action_struct),
        (("task", "mlody/common/huggingface/downloader", "downloader"), task_struct),
    ]

    setf_calls: list[tuple[str, object, str]] = []

    def fake_setf(label: str, value: object, *, workspace: object, source: str) -> None:
        setf_calls.append((label, value, source))

    with patch("mlody.core.setf.setf", side_effect=fake_setf):
        import mlody.core.setf  # noqa: PLC0415
        _normalize_action_implementations(fake_workspace)

    assert [call[0] for call in setf_calls] == [
        "//mlody/common/huggingface/downloader:downloader-action.implementation",
        "//mlody/common/huggingface/downloader:downloader.action.implementation",
    ]
    for _label, value, source in setf_calls:
        assert getattr(value, "kind", None) == "implementation"
        assert getattr(value, "type", None) == "sandbox"
        assert getattr(getattr(value, "build", None), "target", None) == ":model-download"
        assert source == f"DEFAULT: {value}"


def test_normalize_action_implementations_populates_grouped_task_actions() -> None:
    """Grouped task actions inherit sandbox defaults from build-backed actions."""
    from mlody.resolver.resolver import _normalize_action_implementations  # noqa: PLC0415

    model_build = Struct(
        kind="build_ref",
        name="bazel",
        type="bazel",
        target=":model-download",
    )
    info_build = Struct(
        kind="build_ref",
        name="bazel",
        type="bazel",
        target=":metadata-export",
    )
    grouped_actions = {
        "model": Struct(
            kind="action",
            name="download-model",
            implementation=None,
            build=model_build,
        ),
        "info": Struct(
            kind="action",
            name="export-metadata",
            implementation=None,
            build=info_build,
        ),
    }
    task_struct = Struct(
        kind="task",
        name="downloader",
        action=grouped_actions,
    )
    fake_workspace = MagicMock()
    fake_workspace.root_infos = {}
    fake_workspace._workspace_root = Path("/repo")
    fake_workspace._monorepo_root = Path("/repo")
    fake_workspace.registry_view.iter_registry_items.return_value = [
        (("task", "mlody/common/huggingface/downloader", "downloader"), task_struct),
    ]

    setf_calls: list[tuple[str, object, str]] = []

    def fake_setf(label: str, value: object, *, workspace: object, source: str) -> None:
        setf_calls.append((label, value, source))

    with patch("mlody.core.setf.setf", side_effect=fake_setf):
        import mlody.core.setf  # noqa: PLC0415
        _normalize_action_implementations(fake_workspace)

    assert [call[0] for call in setf_calls] == [
        '//mlody/common/huggingface/downloader:downloader.action["model"].implementation',
        '//mlody/common/huggingface/downloader:downloader.action["info"].implementation',
    ]
    assert [getattr(getattr(call[1], "build", None), "target", None) for call in setf_calls] == [
        ":model-download",
        ":metadata-export",
    ]
    assert all(getattr(call[1], "type", None) == "sandbox" for call in setf_calls)
    assert all(call[2] == f"DEFAULT: {call[1]}" for call in setf_calls)



def test_normalize_action_implementations_preserves_explicit_implementations() -> None:
    """Explicit implementations win over build-backed sandbox defaults."""
    from mlody.resolver.resolver import _normalize_action_implementations  # noqa: PLC0415

    build_ref = Struct(
        kind="build_ref",
        name="bazel",
        type="bazel",
        target=":model-download",
    )
    explicit_impl = Struct(
        kind="implementation",
        name="shell_script",
        type="shell_script",
        content="echo hi",
    )
    action_struct = Struct(
        kind="action",
        name="downloader-action",
        implementation=explicit_impl,
        build=build_ref,
    )
    task_struct = Struct(
        kind="task",
        name="downloader",
        action=action_struct,
    )
    fake_workspace = MagicMock()
    fake_workspace.root_infos = {}
    fake_workspace._workspace_root = Path("/repo")
    fake_workspace._monorepo_root = Path("/repo")
    fake_workspace.registry_view.iter_registry_items.return_value = [
        (("action", "mlody/common/huggingface/downloader", "downloader-action"), action_struct),
        (("task", "mlody/common/huggingface/downloader", "downloader"), task_struct),
    ]

    with patch("mlody.core.setf.setf") as mock_setf:
        import mlody.core.setf  # noqa: PLC0415
        _normalize_action_implementations(fake_workspace)

    mock_setf.assert_not_called()
