"""Tests for mlody.resolver.resolver — label parsing, SHA resolution, and factory."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from common.python.starlarkish.core.struct import Struct

from mlody.resolver.errors import (
    AmbiguousRefError,
    BranchTagCollisionError,
    LabelParseError,
    LockBusyError,
    UnknownRefError,
    WorkspaceResolutionError,
)
from mlody.resolver.resolver import (
    ResolvedRef,
    configure_workspace,
    parse_label,
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

    def test_inline_value_target_updates_inline_location_payload(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()
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

    def test_invalid_config_entry_raises_workspace_resolution_error(self) -> None:
        workspace = MagicMock()
        workspace.registry_view.iter_registry_items.return_value = ()

        with pytest.raises(WorkspaceResolutionError, match="LABEL=VALUE"):
            configure_workspace(workspace, ["@lexica//services/release/api/image:image"])

        workspace.resolve.assert_not_called()


class TestResolveWorkspaceCwdPath:
    """Requirement: resolve_workspace cwd passthrough."""

    def test_cwd_label_returns_monorepo_workspace_and_none_sha(
        self, tmp_path: Path
    ) -> None:
        # Scenario: cwd path — label starts with @
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace("@lexica//models:bert", monorepo_root=tmp_path)

        assert sha is None
        assert ws is mock_ws
        mock_ws_cls.assert_called_once_with(
            monorepo_root=tmp_path,
            roots_file=None,
            full_workspace=False,
            print_fn=print,
            extra_roots=None,
            lazy_roots=None,
            workspace_root=None,
        )
        mock_ws.load.assert_called_once()

    def test_double_slash_label_returns_cwd_workspace(self, tmp_path: Path) -> None:
        with patch("mlody.resolver.resolver.Workspace") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws_cls.return_value = mock_ws

            ws, sha = resolve_workspace("//models:bert", monorepo_root=tmp_path)

        assert sha is None

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
        assert ws is mock_ws
        # Workspace constructed from the cache dir
        dest = cache_root / full_sha
        mock_ws_cls.assert_called_once_with(
            monorepo_root=dest,
            roots_file=None,
            full_workspace=False,
            print_fn=print,
        )
        mock_ws.load.assert_called_once()

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
