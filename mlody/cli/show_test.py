"""Tests for mlody.cli.show — show subcommand and show_fn."""

from __future__ import annotations

import functools
import http.server
from io import StringIO
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import networkx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rich.console import Console
from click.testing import CliRunner
from common.python.starlarkish.core.struct import Struct, struct

import mlody.cli.show
from mlody.cli.dag_render import DagSelectionResult
from mlody.cli.main import cli
from mlody.cli.show import show_fn
from mlody.core.assets.interfaces import MaterializedAsset
from mlody.core.assets.metadata import AssetMetadata
from mlody.core.action_graph_value import ACTION_GRAPH_TYPE
from mlody.core.dag_value import make_dag_virtual_value
from mlody.core.label import parse_label as _parse_label
from mlody.core.virtual_value import make_virtual_value
from mlody.resolver.errors import UnknownRefError, WorkspaceResolutionError
from mlody.resolver.values.internal import _RawAttrValue
from mlody.resolver.values.registry_backed import (
    MlodyActionValue,
    MlodyTaskValue,
    MlodyValueValue,
)
from mlody.resolver.values.structural import (
    MlodyFolderValue,
    MlodySourceRangeValue,
    MlodySourceValue,
    MlodyUnresolvedValue,
    MlodyVectorValue,
)


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    """Serve *tmp_path* over HTTP and return ``(base_url, root)``."""
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", tmp_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _make_type_struct(
    name: str,
    *,
    root_kind: str | None = None,
    attributes: dict[str, object] | None = None,
) -> Struct:
    return Struct(
        kind="type",
        type=name,
        name=name,
        _root_kind=root_kind or name,
        attributes=attributes or {},
        _allowed_attrs={},
    )


def _remote_asset(path: Path, *, uri: str, content_hash: str) -> MaterializedAsset:
    return MaterializedAsset(
        path=path,
        content_hash=content_hash,
        metadata=AssetMetadata(
            uri=uri,
            resolved_url=uri,
            digest=None,
            digest_type=None,
            length=None,
            update_time=None,
            transport="http",
        ),
    )


def _attach_registered_user(
    workspace: object,
    *,
    name: str = "mav",
    description: str = "Maurizio Vitale",
) -> None:
    workspace.evaluator.registry.users.by_name = {
        name: struct(
            kind="user",
            name=name,
            description=description,
            groups=["admin"],
        )
    }


def _set_runtime_user(workspace: object, user: str) -> None:
    workspace.evaluator._extra_ctx = struct(workspace=struct(user=user))


# ---------------------------------------------------------------------------
# show_fn — functional form
# ---------------------------------------------------------------------------


class TestShowFn:
    """Requirement: show_fn accepts a label and resolves via resolve_workspace."""

    def test_single_cwd_label_resolves_mlody_value(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        expected_value = MlodyTaskValue(struct=struct(name="lr", kind="task"))

        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = expected_value
            result = show_fn("@bert//models:lr", monorepo_root=tmp_path)

        assert result is expected_value

    def test_resolve_workspace_called_with_label_and_root(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        expected_value = MlodySourceValue(path="models/lr")

        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = expected_value
            show_fn("@bert//models:lr", monorepo_root=tmp_path)

        mock_rw.assert_called_once_with(
            "@bert//models:lr",
            monorepo_root=tmp_path,
            workspace_root=None,
            roots_file=None,
            full_workspace=False,
            print_fn=print,
            verbose=False,
        )

    def test_resolve_label_to_value_called_with_concrete_label(self, tmp_path: Path) -> None:
        # After workspace resolution, resolve_label_to_value is called with the
        # parsed concrete label and workspace.
        mock_ws = MagicMock()
        expected_value = MlodySourceValue(path="models/lr")

        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = expected_value
            show_fn("@bert//models:lr", monorepo_root=tmp_path)

        mock_rlv.assert_called_once()
        call_args = mock_rlv.call_args
        # First arg is the parsed Label object
        assert call_args.args[1] is mock_ws

    def test_committoid_label_uses_inner_label_for_resolver(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        expected_value = MlodySourceValue(path="models/lr")

        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, "a" * 40)
            mock_rlv.return_value = expected_value
            show_fn("main|@bert//models:lr", monorepo_root=tmp_path)

        # resolve_label_to_value must be called with the inner label (not committoid-qualified)
        mock_rlv.assert_called_once()
        call_args = mock_rlv.call_args
        label_arg = call_args.args[0]
        # The label entity root should be "bert", not "main"
        assert label_arg.entity is not None
        assert label_arg.entity.root == "bert"


# ---------------------------------------------------------------------------
# CLI show command — cwd target
# ---------------------------------------------------------------------------


class TestShowCommandCwdTarget:
    """Requirement: cwd target resolves against cwd workspace."""

    def test_cwd_target_resolves_and_prints(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:lr"]
        # New path: resolve_label_to_value returns a MlodyTaskValue
        task_struct = struct(kind="task", name="lr")
        resolved_value = MlodyTaskValue(struct=task_struct)

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        # task rendering includes "task:" prefix
        assert "task" in result.output or "lr" in result.output

    def test_cwd_target_passes_config_overrides_to_resolve_workspace(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:lr"]
        resolved_value = MlodyTaskValue(struct=struct(kind="task", name="lr"))

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "--with", "@bert//models:cfg=abc123", "@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        mock_rw.assert_called_once_with(
            "@bert//models:lr",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            config=("@bert//models:cfg=abc123",),
            user="mav",
            roots_file=None,
            full_workspace=False,
            verbose=False,
        )

    def test_cwd_target_with_legacy_workspace_injection(self) -> None:
        # Existing tests inject workspace — this legacy path must still work
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output

    def test_cwd_target_with_legacy_workspace_applies_config_overrides(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        with patch("mlody.cli.show.configure_workspace") as mock_configure:
            mock_configure.return_value = ws
            result = runner.invoke(
                cli,
                ["show", "--with", "@bert//models:cfg=abc123", "@bert//models:lr"],
                obj={"workspace": ws, "verbose": False},
            )

        assert result.exit_code == 0
        mock_configure.assert_called_once_with(ws, ("@bert//models:cfg=abc123",))

    def test_repeated_legacy_workspace_invocations_reconfigure_same_baseline(self) -> None:
        baseline = MagicMock()
        baseline.root_infos = {}
        _attach_registered_user(baseline)

        request_a = MagicMock()
        request_a.root_infos = {}
        request_a.resolve.return_value = "first"

        request_b = MagicMock()
        request_b.root_infos = {}
        request_b.resolve.return_value = "second"

        runner = CliRunner()
        with patch(
            "mlody.cli.show.configure_workspace",
            side_effect=[request_a, request_b],
        ) as mock_configure:
            result_a = runner.invoke(
                cli,
                ["show", "--with", "@bert//models:cfg=first", "@bert//models:lr"],
                obj={"workspace": baseline, "verbose": False},
            )
            result_b = runner.invoke(
                cli,
                ["show", "--with", "@bert//models:cfg=second", "@bert//models:lr"],
                obj={"workspace": baseline, "verbose": False},
            )

        assert result_a.exit_code == 0
        assert result_b.exit_code == 0
        assert "first" in result_a.output
        assert "second" in result_b.output
        request_a.resolve.assert_called_once_with("@bert//models:lr")
        request_b.resolve.assert_called_once_with("@bert//models:lr")
        assert mock_configure.call_count == 2
        mock_configure.assert_any_call(baseline, ("@bert//models:cfg=first",))
        mock_configure.assert_any_call(baseline, ("@bert//models:cfg=second",))


# ---------------------------------------------------------------------------
# CLI show command — committoid target
# ---------------------------------------------------------------------------


class TestShowCommandCommittoidTarget:
    """Requirement: committoid-qualified target resolves against cached workspace."""

    def test_committoid_target_calls_resolve_workspace_with_full_label(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "result"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.return_value = (mock_ws, "a" * 40)
            result = runner.invoke(
                cli,
                ["show", "main|@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        mock_rw.assert_called_once_with(
            "main|@bert//models:lr",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            config=(),
            user="mav",
            roots_file=None,
            full_workspace=False,
            verbose=False,
        )

    def test_committoid_target_calls_resolve_label_to_value_with_inner_label(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:lr"]
        resolved_value = MlodySourceValue(path="models/lr")

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, "a" * 40)
            mock_rlv.return_value = resolved_value
            runner.invoke(
                cli,
                ["show", "main|@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        # resolve_label_to_value must be called with inner label entity (not committoid)
        mock_rlv.assert_called_once()
        label_arg = mock_rlv.call_args.args[0]
        assert label_arg.entity is not None
        assert label_arg.entity.root == "bert"


# ---------------------------------------------------------------------------
# CLI show command — mixed targets
# ---------------------------------------------------------------------------


class TestShowCommandMixedTargets:
    """Requirement: mixed cwd and committoid targets coexist."""

    def test_mixed_targets_printed_in_order(self, tmp_path: Path) -> None:
        mock_ws_cwd = MagicMock()
        mock_ws_cwd.root_infos = {}
        mock_ws_cwd.expand_wildcard_label.return_value = ["@bert//models:lr"]
        mock_ws_commit = MagicMock()
        mock_ws_commit.root_infos = {}
        mock_ws_commit.expand_wildcard_label.return_value = ["@bert//models:lr"]

        cwd_value = MlodySourceValue(path="from-cwd")
        commit_value = MlodySourceValue(path="from-main")

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.side_effect = [(mock_ws_cwd, None), (mock_ws_commit, "a" * 40)]
            mock_rlv.side_effect = [cwd_value, commit_value]
            result = runner.invoke(
                cli,
                ["show", "@bert//models:lr", "main|@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        cwd_pos = result.output.index("from-cwd")
        commit_pos = result.output.index("from-main")
        assert cwd_pos < commit_pos


# ---------------------------------------------------------------------------
# CLI show command — verbose mode
# ---------------------------------------------------------------------------


class TestShowCommandVerbose:
    """Requirement: verbose mode emits resolved SHA at DEBUG level."""

    def test_verbose_logs_resolved_sha(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        full_sha = "a" * 40
        mock_ws = MagicMock()
        mock_ws.resolve.return_value = "val"
        mock_ws.root_infos = {}

        runner = CliRunner()
        with caplog.at_level(logging.DEBUG, logger="mlody.cli.show"):
            with patch("mlody.cli.show.resolve_workspace") as mock_rw:
                mock_rw.return_value = (mock_ws, full_sha)
                runner.invoke(
                    cli,
                    ["--verbose", "show", "main|@bert//models:lr"],
                    obj={"monorepo_root": tmp_path, "roots": None, "verbose": True},
                )

        assert any(full_sha in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI show command — --as user selection
# ---------------------------------------------------------------------------


class TestShowCommandAsUser:
    def test_default_user_is_used_when_as_is_omitted(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:lr"]
        resolved_value = MlodyTaskValue(struct=struct(kind="task", name="lr"))

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "Value for user 'mav'" in result.output
        mock_rw.assert_called_once_with(
            "@bert//models:lr",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            config=(),
            user="mav",
            roots_file=None,
            full_workspace=False,
            verbose=False,
        )

    def test_as_accepts_full_description_and_prints_canonical_user(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:lr"]
        _set_runtime_user(mock_ws, "agarcia")
        resolved_value = MlodyTaskValue(struct=struct(kind="task", name="lr"))

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, "a" * 40)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "--as", "Ava Garcia", "@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "Value for user 'agarcia'" in result.output
        assert result.output.index("Value for user 'agarcia'") < result.output.index("lr")
        mock_rw.assert_called_once_with(
            "@bert//models:lr",
            monorepo_root=tmp_path,
            workspace_root=tmp_path,
            config=(),
            user="Ava Garcia",
            roots_file=None,
            full_workspace=False,
            verbose=False,
        )

    def test_as_rejects_unknown_user_before_rendering(
        self, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        with patch(
            "mlody.cli.show.resolve_workspace",
            side_effect=WorkspaceResolutionError(
                "User 'nobody' is not one of the valid registered users. "
                "Valid users: agarcia (Ava Garcia), jlee (Jordan Lee)"
            ),
        ) as mock_rw, patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            result = runner.invoke(
                cli,
                [
                    "show",
                    "--as",
                    "nobody",
                    "@bert//models:lr",
                ],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "nobody" in result.output
        assert "agarcia (Ava Garcia)" in result.output
        assert "jlee (Jordan Lee)" in result.output
        mock_rlv.assert_not_called()
        mock_rw.assert_called_once()


# ---------------------------------------------------------------------------
# CLI show command — output rendering
# ---------------------------------------------------------------------------


class TestShowCommandOutput:
    """Requirement: Resolve and display target values."""

    def test_primitive_value_displayed_as_plain_string(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output

    def test_struct_value_displayed_via_pretty_repr(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = struct(name="bert", lr=0.001)
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:config"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "bert" in result.output
        assert "0.001" in result.output

    def test_virtual_value_is_materialized_before_display(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["'info"]
        info_type = _make_type_struct(
            "mlody_workspace_info",
            root_kind="record",
            attributes={
                "fields": [
                    struct(name="branch", type=_make_type_struct("string")),
                    struct(name="sha", type=_make_type_struct("string")),
                ]
            },
        )
        resolved_value = MlodyValueValue(
            struct=make_virtual_value(
                value_type=info_type,
                label="'info",
                materializer=lambda _value: struct(branch="release", sha="abc123"),
                name="info",
            )
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "'info"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "release" in result.output
        assert "abc123" in result.output
        assert "materializer" not in result.output

    def test_virtual_scalar_value_skips_render_dispatch_and_shows_materialized_payload(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:trainer.raw"]
        mock_ws.evaluator = MagicMock()
        mock_ws.evaluator._method_registry = {"render_value": {"methods": [object()]}}
        hash_type = _make_type_struct("string", root_kind="string")
        resolved_value = MlodyValueValue(
            struct=make_virtual_value(
                value_type=hash_type,
                label="@bert//models:trainer.raw",
                materializer=lambda _value: "abc123",
                name="raw",
            )
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv, \
             patch("mlody.core.multimethod.dispatch") as mock_dispatch:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@bert//models:trainer.raw"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "abc123" in result.output
        assert "virtual" not in result.output
        mock_dispatch.assert_not_called()

    def test_dag_virtual_value_matches_removed_dag_command_title(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        target = "@common//huggingface/downloader:downloader.outputs.model.dag"
        mock_ws.expand_wildcard_label.return_value = [target]
        resolved_value = MlodyValueValue(
            struct=make_dag_virtual_value(mock_ws, "model", target)
        )
        dag = networkx.MultiDiGraph()

        runner = CliRunner()
        with (
            patch("mlody.cli.show.resolve_workspace") as mock_rw,
            patch("mlody.cli.show._maybe_print_dag_plan"),
            patch("mlody.cli.show.resolve_label_to_value") as mock_rlv,
            patch("mlody.core.virtual_value.force_virtual_value", return_value=dag),
            patch(
                "mlody.cli.show.build_dag_table",
                return_value="DAG-TABLE",
            ) as mock_build,
            patch.object(mlody.cli.show._console, "print") as mock_print,
        ):
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", target],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert result.output == "Value for user 'mav'\n"
        mock_build.assert_called_once_with(
            dag,
            "DAG — ancestors of '@common//huggingface/downloader:downloader.outputs.model'",
        )
        mock_print.assert_called_once_with("DAG-TABLE")

    def test_raw_value_is_rendered_as_json_blob(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:cfg.raw"]
        raw_type = _make_type_struct("string", root_kind="string")
        resolved_value = MlodyValueValue(
            struct=make_virtual_value(
                value_type=raw_type,
                label="@bert//models:cfg.raw",
                materializer=lambda _value: json.dumps(
                    {"kind": "task", "name": "trainer", "state": "ready"},
                    indent=2,
                    sort_keys=True,
                ),
                name="raw",
            )
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@bert//models:cfg.raw"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert '"kind": "task"' in result.output
        assert '"name": "trainer"' in result.output
        assert '"state": "ready"' in result.output

    def test_structured_inline_value_renders_inline_payload_json(
        self, tmp_path: Path
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = [
            "@common//huggingface/downloader:downloader.config.ctx"
        ]
        mock_ws.evaluator = MagicMock()
        mock_ws.evaluator._method_registry = {}
        ctx_type = _make_type_struct(
            "mlody-task-context",
            root_kind="record",
            attributes={
                "fields": [
                    struct(name="file", type=_make_type_struct("string")),
                    struct(
                        name="workspace",
                        type=_make_type_struct("mlody-task-context.workspace"),
                    ),
                    struct(
                        name="run",
                        type=_make_type_struct("mlody-task-context.run"),
                    ),
                ]
            },
        )
        payload = struct(
            file="/repo/mlody/common/huggingface/downloader.mlody",
            workspace=struct(user="mav", branch="main"),
            run=struct(id="run-1", user="mav"),
        )
        resolved_value = MlodyValueValue(
            struct=Struct(
                kind="value",
                name="ctx",
                type=ctx_type,
                location=Struct(kind="location", type="inline", name="inline", data=payload),
                default=payload,
            )
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@common//huggingface/downloader:downloader.config.ctx"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert '"file": "/repo/mlody/common/huggingface/downloader.mlody"' in result.output
        assert '"workspace"' in result.output
        assert '"run"' in result.output
        assert "_synthetic_task_ctx" not in result.output

    def test_describe_empty_vector_is_non_empty(self) -> None:
        assert (
            mlody.cli.show._describe_mlody_value(MlodyVectorValue(elements=()))
            == "value:\n(empty vector)"
        )

    def test_multiple_targets_displayed_in_order(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, "adam"]
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@bert//models:lr", "@bert//models:opt"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 0
        lr_pos = result.output.index("0.001")
        opt_pos = result.output.index("adam")
        assert lr_pos < opt_pos


    def test_sql_row_list_is_rendered_as_tabular_preview(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:rows"]
        resolved_value = _RawAttrValue(
            value=[
                {"name": "Alice", "salary": 120000},
                {"name": "Bob", "salary": 90000},
            ],
            label=_parse_label("@bert//models:rows[@sql WHERE salary > 0]"),
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@bert//models:rows[@sql WHERE salary > 0]"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "pyarrow.Table" in result.output
        assert "Alice" in result.output
        assert "salary" in result.output
        assert "[0]" not in result.output
        assert "name: Alice" not in result.output

    def test_tabular_preview_with_textual_images_keeps_rich_table(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@pixelle//datasets:celebA"]
        resolved_value = _RawAttrValue(
            value=pa.Table.from_pylist(
                [
                    {"image": {"bytes": b"fake-image"}, "Young": False},
                    {"image": {"bytes": b"fake-image-2"}, "Young": True},
                ]
            ),
            label=_parse_label("@pixelle//datasets:celebA[@sql select image,Young]"),
        )

        runner = CliRunner()
        fake_image = type(
            "FakeImage",
            (),
            {"format": "PNG", "width": 178, "height": 218},
        )()
        with (
            patch("mlody.cli.show.resolve_workspace") as mock_rw,
            patch("mlody.cli.show.resolve_label_to_value") as mock_rlv,
            patch("mlody.cli.show._image_encoder_for_terminal", return_value=lambda _img: "<IMG>"),
            patch("mlody.cli.show._to_pil_image", return_value=fake_image),
        ):
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@pixelle//datasets:celebA[@sql select image,Young]"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "pyarrow.Table" in result.output
        assert "Young" in result.output
        assert "False" in result.output
        assert "True" in result.output
        assert "<IMG>" in result.output
        assert "[0]" not in result.output

    def test_wide_tabular_preview_falls_back_to_row_mode(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@pixelle//datasets:celebA"]
        wide_rows = [
            {f"col_{i}": (i % 2 == 0) for i in range(20)},
            {f"col_{i}": (i % 3 == 0) for i in range(20)},
        ]
        resolved_value = _RawAttrValue(
            value=pa.Table.from_pylist(wide_rows),
            label=_parse_label("@pixelle//datasets:celebA[@sql where flag=True limit 2]"),
        )

        runner = CliRunner()
        with (
            patch("mlody.cli.show.resolve_workspace") as mock_rw,
            patch("mlody.cli.show.resolve_label_to_value") as mock_rlv,
        ):
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@pixelle//datasets:celebA[@sql where flag=True limit 2]"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "pyarrow.Table" in result.output
        assert "[0]" in result.output
        assert "col_0: True" in result.output
        assert "col_19: False" in result.output
        assert "┏" not in result.output

    def test_tabular_preview_with_unsafe_terminal_images_falls_back(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@pixelle//datasets:celebA"]
        resolved_value = _RawAttrValue(
            value=pa.Table.from_pylist(
                [
                    {"image": {"bytes": b"fake-image"}, "Young": False},
                    {"image": {"bytes": b"fake-image-2"}, "Young": True},
                ]
            ),
            label=_parse_label("@pixelle//datasets:celebA[@sql select image,Young]"),
        )

        runner = CliRunner()
        fake_image = type(
            "FakeImage",
            (),
            {"format": "PNG", "width": 178, "height": 218},
        )()
        with (
            patch("mlody.cli.show.resolve_workspace") as mock_rw,
            patch("mlody.cli.show.resolve_label_to_value") as mock_rlv,
            patch(
                "mlody.cli.show._image_encoder_for_terminal",
                return_value=mlody.cli.show._TerminalImageEncoder(
                    encode=lambda _img: "\x1b_Gunsafe\x1b\\",
                    supports_rich_tables=False,
                ),
            ),
            patch("mlody.cli.show._to_pil_image", return_value=fake_image),
        ):
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@pixelle//datasets:celebA[@sql select image,Young]"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "pyarrow.Table" in result.output
        assert "[0]" in result.output
        assert "Young: False" in result.output
        assert "Young: True" in result.output

    def test_tabular_preview_with_kitty_style_images_keeps_rich_table(self, tmp_path: Path) -> None:
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@pixelle//datasets:celebA"]
        resolved_value = _RawAttrValue(
            value=pa.Table.from_pylist(
                [
                    {"image": {"bytes": b"fake-image"}, "Young": False},
                    {"image": {"bytes": b"fake-image-2"}, "Young": True},
                ]
            ),
            label=_parse_label("@pixelle//datasets:celebA[@sql select image,Young]"),
        )

        runner = CliRunner()
        fake_image = type(
            "FakeImage",
            (),
            {"format": "PNG", "width": 178, "height": 218},
        )()
        with (
            patch("mlody.cli.show.resolve_workspace") as mock_rw,
            patch("mlody.cli.show.resolve_label_to_value") as mock_rlv,
            patch(
                "mlody.cli.show._image_encoder_for_terminal",
                return_value=mlody.cli.show._TerminalImageEncoder(
                    encode=lambda _img: "\x1b_Gkitty\x1b\\",
                    supports_rich_tables=True,
                ),
            ),
            patch("mlody.cli.show._to_pil_image", return_value=fake_image),
        ):
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = resolved_value
            result = runner.invoke(
                cli,
                ["show", "@pixelle//datasets:celebA[@sql select image,Young]"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "pyarrow.Table" in result.output
        assert "Young" in result.output
        assert "False" in result.output
        assert "True" in result.output
        assert "[0]" not in result.output
        assert "<PNG" not in result.output

    def test_kitty_encoder_uses_explicit_table_placement(self) -> None:
        class _FakeImage:
            size = (32, 32)

            def convert(self, _mode: str):
                return self

            def save(self, handle, *, format: str, optimize: bool) -> None:
                del format, optimize
                handle.write(b"fake-png")

        encoder = mlody.cli.show._TerminalImageEncoder(
            encode=lambda img: mlody.cli.show._kitty_encode(
                img,
                max_width=160,
                cell_rows=4,
                no_cursor_movement=True,
            ),
            encode_with_placement=lambda img, columns, rows: mlody.cli.show._kitty_encode(
                img,
                max_width=160,
                cell_columns=columns,
                cell_rows=rows,
                cell_aspect=2.0,
                no_cursor_movement=True,
            ),
            supports_rich_tables=True,
            rich_table_target_rows=4,
            rich_table_cell_aspect=2.0,
        )
        encoded = encoder.encode_for_table(_FakeImage(), columns=8, rows=4)

        assert encoded is not None
        header = encoded.split(";", 1)[0]
        assert "r=4" in header
        assert "c=8" in header
        assert "C=1" in header

    def test_rich_table_image_cell_reserves_4x4_character_block(self) -> None:
        renderable = mlody.cli.show._RichTableImageCell(
            encoded="\x1b_Gkitty\x1b\\",
            width=4,
            height=4,
        )
        console = Console(file=StringIO(), width=20, force_terminal=False, color_system=None)
        measurement = renderable.__rich_measure__(console, console.options)

        console.print(renderable, end="")
        rendered = console.file.getvalue()

        assert measurement.minimum == 4
        assert measurement.maximum == 4
        assert rendered.splitlines() == ["    ", "    ", "    ", "    "]

    def test_prepare_cell_uses_squareish_kitty_table_footprint(self) -> None:
        class _FakeImage:
            format = "PNG"
            width = 178
            height = 218
            size = (178, 218)

            def convert(self, _mode: str):
                return self

            def save(self, handle, *, format: str, optimize: bool) -> None:
                del format, optimize
                handle.write(b"fake-png")

        fake_image = _FakeImage()
        encoder = mlody.cli.show._TerminalImageEncoder(
            encode=lambda _img: "\x1b_Gkitty\x1b\\",
            encode_with_placement=lambda _img, columns, rows: f"\x1b_Gc={columns},r={rows},C=1;stub\x1b\\",
            supports_rich_tables=True,
            rich_table_target_rows=4,
            rich_table_cell_aspect=2.0,
        )

        with patch("mlody.cli.show._to_pil_image", return_value=fake_image):
            cell = mlody.cli.show._prepare_cell(
                {"bytes": b"fake-image"},
                image_encoder=encoder,
            )

        assert cell.encoded is not None
        header = cell.encoded.split(";", 1)[0]
        assert "c=7" in header
        assert "r=4" in header
        assert "C=1" in header
        assert cell.display_height == 4
        assert cell.display_width == 7


class TestShowCommandDagPlan:
    """Requirement: output labels render the same DAG table used by dag."""

    def test_output_label_renders_pruned_dag_table(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = "model-value"
        ws.root_infos = {}
        _attach_registered_user(ws)

        dag = networkx.MultiDiGraph()
        dag.add_node("task/common/downloader:download")

        runner = CliRunner()
        with (
            patch("mlody.cli.show.build_dag", return_value=dag),
            patch(
                "mlody.cli.show.resolve_show_output_selection",
                return_value=DagSelectionResult(graph=dag, resolved_label="model"),
            ),
            patch("mlody.cli.show.render_dag_table") as mock_render,
        ):
            result = runner.invoke(
                cli,
                ["show", "@common//huggingface/downloader:downloader.outputs.model"],
                obj={"workspace": ws, "verbose": False},
            )

        assert result.exit_code == 0
        mock_render.assert_called_once_with(
            dag,
            "DAG — ancestors of '@common//huggingface/downloader:downloader.outputs.model'",
            console=mlody.cli.show._console,
        )
        ws.resolve.assert_called_once_with(
            "@common//huggingface/downloader:downloader.outputs.model"
        )

    def test_non_output_label_skips_dag_table_render(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = "ok"
        ws.root_infos = {}
        _attach_registered_user(ws)

        dag = networkx.MultiDiGraph()

        runner = CliRunner()
        with (
            patch("mlody.cli.show.build_dag", return_value=dag),
            patch("mlody.cli.show.resolve_show_output_selection", return_value=None),
            patch("mlody.cli.show.render_dag_table") as mock_render,
        ):
            result = runner.invoke(
                cli,
                ["show", "@common//huggingface/downloader:downloader"],
                obj={"workspace": ws, "verbose": False},
            )

        assert result.exit_code == 0
        mock_render.assert_not_called()


class TestShowCommandLineageRendering:
    def test_lineage_renders_source_and_value_columns(self) -> None:
        lineage_type = _make_type_struct(
            "vector",
            root_kind="vector",
            attributes={
                "element_type": _make_type_struct(
                    "mlody-lineage-event",
                    root_kind="record",
                )
            },
        )
        lineage_value = MlodyValueValue(
            struct=Struct(
                kind="value",
                name="lineage",
                type=lineage_type,
            ),
        )
        lineage_events = [
            Struct(
                kind="lineage_event",
                source="DEFAULT: foo",
                new_value=Struct(kind="location", data="foo"),
            ),
            Struct(
                kind="lineage_event",
                source="COMMAND_LINE: //simple:a-string=bar",
                new_value=Struct(kind="location", data="bar"),
            ),
        ]

        with patch("mlody.cli.show.force", return_value=lineage_events):
            node = mlody.cli.show._render_mlody_value(lineage_value)

        renderable = node.render(SimpleNamespace(console=Console(file=StringIO())))

        assert renderable.title == "lineage"
        table = renderable.renderable
        assert table.columns[0].header == "source"
        assert table.columns[1].header == "value"
        assert [cell.plain for cell in table.columns[0]._cells] == ["default", "user"]
        assert [cell.plain for cell in table.columns[1]._cells] == ["foo", "bar"]

    def test_lineage_renders_transfer_details_under_value(self) -> None:
        lineage_type = _make_type_struct(
            "vector",
            root_kind="vector",
            attributes={
                "element_type": _make_type_struct(
                    "mlody-lineage-event",
                    root_kind="record",
                )
            },
        )
        lineage_value = MlodyValueValue(
            struct=Struct(
                kind="value",
                name="lineage",
                type=lineage_type,
            ),
        )
        lineage_events = [
            Struct(
                kind="lineage_event",
                source="downloaded from",
                new_value=Struct(kind="location", data="https://example.com/employees.csv"),
                details={
                    "kind": "remote-download",
                    "staged_path": "/tmp/mlody-remote-abc.csv",
                    "content_hash": "abc123",
                },
            )
        ]

        with patch("mlody.cli.show.force", return_value=lineage_events):
            node = mlody.cli.show._render_mlody_value(lineage_value)

        renderable = node.render(SimpleNamespace(console=Console(file=StringIO())))

        table = renderable.renderable
        assert [cell.plain for cell in table.columns[0]._cells] == ["downloaded from"]
        assert (
            table.columns[1]._cells[0].plain
            == "content of /tmp/mlody-remote-abc.csv\n"
            "kind: remote-download\n"
            "staged_path: /tmp/mlody-remote-abc.csv\n"
            "content_hash: abc123"
        )

    def test_lineage_hides_internal_chain_metadata(self) -> None:
        lineage_type = _make_type_struct(
            "vector",
            root_kind="vector",
            attributes={
                "element_type": _make_type_struct(
                    "mlody-lineage-event",
                    root_kind="record",
                )
            },
        )
        lineage_value = MlodyValueValue(
            struct=Struct(
                kind="value",
                name="lineage",
                type=lineage_type,
            ),
        )
        lineage_events = [
            Struct(
                kind="lineage_event",
                source="CONFIG: xxx: //simple:a-string=FOOBAR",
                new_value=Struct(kind="location", data="FOOBAR"),
                details={
                    "previous_owner_hash": "abc123",
                    "content_hash": "def456",
                },
            )
        ]

        with patch("mlody.cli.show.force", return_value=lineage_events):
            node = mlody.cli.show._render_mlody_value(lineage_value)

        renderable = node.render(SimpleNamespace(console=Console(file=StringIO())))

        table = renderable.renderable
        assert [cell.plain for cell in table.columns[0]._cells] == ["config"]
        assert table.columns[1]._cells[0].plain == "FOOBAR\ncontent_hash: def456"

    def test_source_range_uses_syntax_highlighted_source_panel(
        self,
        tmp_path: Path,
    ) -> None:
        source_path = tmp_path / "pipeline.mlody"
        source_path.write_text(
            "x = 1\n"
            "cached_value(name=\"raw-employees-remote\")\n"
            "y = 2\n"
        )
        node = mlody.cli.show._render_mlody_value(
            MlodySourceRangeValue(
                filepath="pipeline.mlody",
                abs_path=source_path,
                start_line=2,
                end_line=2,
            )
        )
        assert isinstance(node, mlody.cli.show.SyntaxNode)
        assert node.language == "python"
        assert "# pipeline.mlody:2" in node.value
        assert "cached_value(name=\"raw-employees-remote\")" in node.value


# ---------------------------------------------------------------------------
# CLI show command — error handling
# ---------------------------------------------------------------------------


class TestShowCommandErrors:
    """Requirement: Clear error messages for resolution failures."""

    def test_missing_root_shows_error_with_available_roots(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = KeyError("NONEXISTENT")
        ws.root_infos = {"lexica": MagicMock(), "common": MagicMock()}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@NONEXISTENT//pkg:x"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 1
        assert "NONEXISTENT" in result.stderr
        assert "Available roots:" in result.stderr
        assert "lexica" in result.stderr

    def test_missing_field_shows_error(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = AttributeError("'Struct' object has no attribute 'bad_field'")
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:bad_field"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 1
        assert "bad_field" in result.stderr

    def test_partial_failure_shows_successes_and_errors(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, KeyError("MISSING")]
        ws.root_infos = {}
        _attach_registered_user(ws)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@bert//models:lr", "@MISSING//pkg:x"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 1
        assert "0.001" in result.output
        assert "MISSING" in result.stderr

    def test_workspace_resolution_error_printed_to_stderr_and_exit_1(
        self, tmp_path: Path
    ) -> None:
        # Scenario: resolver exception causes target to print error and continue
        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.side_effect = UnknownRefError("nosuchref", "origin")
            result = runner.invoke(
                cli,
                ["show", "nosuchref|@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "nosuchref" in result.stderr or "nosuchref" in result.output

    def test_resolver_exception_continues_to_next_target(self, tmp_path: Path) -> None:
        # Scenario: processing continues for remaining targets after resolver error
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//models:good"]
        ok_value = MlodySourceValue(path="models/good")

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.side_effect = [
                UnknownRefError("bad", "origin"),
                (mock_ws, None),
            ]
            mock_rlv.return_value = ok_value
            result = runner.invoke(
                cli,
                ["show", "bad|@bert//models:lr", "@bert//models:good"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "source" in result.output or "models/good" in result.output


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


class TestShowRegistration:
    """Requirement: main() imports show to register subcommand."""

    def test_show_appears_in_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "show" in result.output


# ---------------------------------------------------------------------------
# Tasks 7.1–7.6: end-to-end show rendering per MlodyValue type
# Requirement: show command — Label→Value pipeline integration
# ---------------------------------------------------------------------------


def _make_show_runner(
    tmp_path: Path,
    resolved_value: object,
    target: str = "@bert//models:lr",
) -> object:
    """Helper: invoke show with resolve_label_to_value mocked to return resolved_value."""
    mock_ws = MagicMock()
    mock_ws.root_infos = {}
    mock_ws.expand_wildcard_label.return_value = [target]

    runner = CliRunner()
    with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
         patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
        mock_rw.return_value = (mock_ws, None)
        mock_rlv.return_value = resolved_value
        return runner.invoke(
            cli,
            ["show", target],
            obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
        )


class TestShowMlodyValueRendering:
    """Requirement: show renders each MlodyValue kind and exits 0/1 correctly."""

    def test_show_renders_folder_value_exits_0(self, tmp_path: Path) -> None:
        """Task 7.1 — Scenario: show renders MlodyFolderValue."""
        value = MlodyFolderValue(path="pkg/mydir", children=["a.mlody", "b.mlody"])
        result = _make_show_runner(tmp_path, value, target="@bert//pkg/mydir")

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "pkg/mydir" in result.output  # type: ignore[union-attr]

    def test_show_renders_source_value_exits_0(self, tmp_path: Path) -> None:
        """Task 7.2 — Scenario: show renders MlodySourceValue."""
        value = MlodySourceValue(path="pkg/foo")
        result = _make_show_runner(tmp_path, value, target="@bert//pkg/foo")

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "pkg/foo" in result.output  # type: ignore[union-attr]

    def test_show_renders_task_value_exits_0(self, tmp_path: Path) -> None:
        """Task 7.3 — Scenario: show renders MlodyTaskValue."""
        value = MlodyTaskValue(struct=struct(kind="task", name="my_task"))
        result = _make_show_runner(tmp_path, value, target="@bert//pkg/foo:my_task")

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "task" in result.output  # type: ignore[union-attr]

    def test_show_renders_materialized_virtual_scalar_value(self, tmp_path: Path) -> None:
        """Regression: virtual scalar leaves like task.raw render their materialized payload."""
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@common//huggingface/downloader:downloader.raw"]
        mock_ws.evaluator = MagicMock()
        mock_ws.evaluator._method_registry = {"render_value": {"methods": [object()]}}
        value = MlodyValueValue(
            struct=make_virtual_value(
                value_type=_make_type_struct("string", root_kind="string"),
                label="@common//huggingface/downloader:downloader.raw",
                materializer=lambda _value: "abc123",
                name="raw",
            )
        )

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = value
            result = runner.invoke(
                cli,
                ["show", "@common//huggingface/downloader:downloader.raw"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 0
        assert "abc123" in result.output
        assert "virtual" not in result.output
        assert "Location" not in result.output

    def test_show_renders_aggregate_output_types_for_downloader(self, tmp_path: Path) -> None:
        """Aggregate outputs include element-type detail in the rendered task table."""
        string_type = _make_type_struct("string", root_kind="string")
        vector_string = _make_type_struct(
            "vector",
            root_kind="vector",
            attributes={"element_type": string_type},
        )
        task_struct = Struct(
            kind="task",
            name="downloader",
            inputs={},
            config={},
            outputs={
                "model": Struct(
                    name="model",
                    type=_make_type_struct("nothing", root_kind="nothing"),
                    source=Struct(type="inline"),
                    default=None,
                ),
                "committoid": Struct(
                    name="committoid",
                    type=_make_type_struct("nothing", root_kind="nothing"),
                    source=Struct(type="inline"),
                    default=None,
                ),
                "releases": Struct(
                    name="releases",
                    type=vector_string,
                    source=Struct(type="inline"),
                    default=None,
                ),
            },
        )
        value = MlodyTaskValue(struct=task_struct)
        result = _make_show_runner(
            tmp_path,
            value,
            target="@common//huggingface/downloader:downloader",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "releases" in result.output  # type: ignore[union-attr]
        assert "vector[string]" in result.output  # type: ignore[union-attr]

    def test_show_renders_action_value_exits_0(self, tmp_path: Path) -> None:
        """Task 7.4 — Scenario: show renders MlodyActionValue."""
        value = MlodyActionValue(struct=struct(kind="action", name="my_action"))
        result = _make_show_runner(tmp_path, value, target="@bert//pkg/foo:my_action")

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "action" in result.output  # type: ignore[union-attr]

    def test_show_renders_action_graph_virtual_value(self, tmp_path: Path) -> None:
        action_graph = networkx.DiGraph()
        action_graph.add_node(
            "prepare:@common//huggingface/downloader:downloader",
            action=SimpleNamespace(
                title="Prepare Display",
                executor="mlody",
                operation="prepare-show-value",
                detail="@common//huggingface/downloader:downloader",
                description="Consumes the already-resolved requested value and runs show-time preparation.",
                executor_detail="Runs in-process Python in the current mlody CLI/server runtime.",
                payload=SimpleNamespace(
                    before=[
                        SimpleNamespace(
                            name="image",
                            description="oci",
                        )
                    ],
                    after=[],
                    around=[SimpleNamespace(name="bridge")],
                ),
            ),
        )
        value = MlodyValueValue(
            struct=make_virtual_value(
                value_type=ACTION_GRAPH_TYPE,
                label="@common//huggingface/downloader:downloader.agraph",
                materializer=lambda _value: action_graph,
                name="downloader",
            )
        )

        result = _make_show_runner(
            tmp_path,
            value,
            target="@common//huggingface/downloader:downloader.agraph",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "Action Graph" in result.output  # type: ignore[union-attr]
        assert "Payload" in result.output  # type: ignore[union-attr]
        assert "Before:" in result.output  # type: ignore[union-attr]
        assert "image (oci)" in result.output  # type: ignore[union-attr]
        assert "After: —" in result.output  # type: ignore[union-attr]
        assert "Around:" in result.output  # type: ignore[union-attr]
        assert "bridge" in result.output  # type: ignore[union-attr]

    def test_show_exits_1_on_unresolved_value(self, tmp_path: Path) -> None:
        """Task 7.5 — Scenario: show prints red error and exits 1 on MlodyUnresolvedValue."""
        label = _parse_label("@bert//pkg/foo:ghost")
        value = MlodyUnresolvedValue(
            label=label, reason="entity 'ghost' not found in registry"
        )
        mock_ws = MagicMock()
        mock_ws.root_infos = {}
        mock_ws.expand_wildcard_label.return_value = ["@bert//pkg/foo:ghost"]

        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
             patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
            mock_rw.return_value = (mock_ws, None)
            mock_rlv.return_value = value
            result = runner.invoke(
                cli,
                ["show", "@bert//pkg/foo:ghost"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        # Error message contains the reason string
        assert "ghost" in result.stderr or "ghost" in result.output

    def test_show_exits_1_on_workspace_resolution_error(self, tmp_path: Path) -> None:
        """Task 7.6 — Scenario: show exits 1 on WorkspaceResolutionError (existing behavior)."""
        runner = CliRunner()
        with patch("mlody.cli.show.resolve_workspace") as mock_rw:
            mock_rw.side_effect = UnknownRefError("badref", "origin")
            result = runner.invoke(
                cli,
                ["show", "badref|@bert//models:lr"],
                obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
            )

        assert result.exit_code == 1
        assert "badref" in result.stderr or "badref" in result.output


# ---------------------------------------------------------------------------
# Derived value integration tests (Tasks 5.1–5.4)
# Requirement: Resolve and display target values (modified — derived path)
# Requirement: DerivedValueShapeError displayed as a resolution error
# Requirement: MlodyQueryError during materialisation displayed as error
# ---------------------------------------------------------------------------


def _make_derived_location(
    output_path: str,
    sql_fragment: str = "WHERE x > 0",
    *,
    source_paths: list[str] | None = None,
) -> object:
    """Create a Struct that mimics a derived location for CLI tests."""
    attributes: dict[str, object] = {
        "source_ref": ":data",
        "sql_fragment": sql_fragment,
        "dialect": "duckdb",
        "output_path": output_path,
    }
    if source_paths is not None:
        attributes["source_paths"] = source_paths
    return Struct(
        kind="location",
        type="derived",
        name="derived",
        abstract=False,
        _root_kind="derived",
        attributes=attributes,
    )


def _make_value_with_derived_location(
    output_path: str,
    sql_fragment: str = "WHERE x > 0",
    *,
    source_paths: list[str] | None = None,
) -> MlodyValueValue:
    """Return a MlodyValueValue whose location is a derived location struct."""
    loc = _make_derived_location(
        output_path,
        sql_fragment,
        source_paths=source_paths,
    )
    value_struct = Struct(
        kind="value",
        name="derived_val",
        type=None,
        location=loc,
        default=None,
        source=None,
        representation=None,
        _lineage=[],
    )
    return MlodyValueValue(struct=value_struct)


def _make_value_with_plain_location(path: str) -> MlodyValueValue:
    """Return a MlodyValueValue whose location exposes a direct file path."""
    value_struct = Struct(
        kind="value",
        name="plain_table",
        type=None,
        location=Struct(kind="location", type="path", path=path),
        default=None,
        source=None,
        representation=None,
        _lineage=[],
    )
    return MlodyValueValue(struct=value_struct)


def _make_value_with_remote_location(
    uri: str,
    *,
    representation_name: str,
    representation_attributes: dict[str, object],
    name: str = "remote_table",
) -> MlodyValueValue:
    """Return a MlodyValueValue whose location points at a remote URI."""
    representation_fields = {
        "kind": "representation",
        "name": representation_name,
        "attributes": representation_attributes,
    }
    representation_fields.update(representation_attributes)
    value_struct = Struct(
        kind="value",
        name=name,
        type=None,
        location=Struct(
            kind="location",
            type="remote",
            name="remote",
            attributes={"uri": uri},
        ),
        default=None,
        source=None,
        representation=Struct(**representation_fields),
        _lineage=[],
    )
    return MlodyValueValue(struct=value_struct)


def _make_value_with_source_backed_local_location(
    destination_path: str,
    *,
    source_uri: str,
    name: str = "local_table",
) -> MlodyValueValue:
    """Return a local CSV value backed by a remote CSV source value."""
    source_struct = _make_value_with_remote_location(
        source_uri,
        representation_name="csv",
        representation_attributes={
            "separator": ",",
            "header_required": True,
            "multifile": False,
        },
        name="raw_employees",
    ).struct
    value_struct = Struct(
        kind="value",
        name=name,
        type=None,
        location=Struct(kind="location", type="posix", path=destination_path),
        default=None,
        source=":raw_employees",
        _source_value=source_struct,
        representation=source_struct.representation,
        _lineage=[],
    )
    return MlodyValueValue(struct=value_struct)


def _invoke_show_with_derived(
    tmp_path: Path,
    resolved_value: object,
    target: str = "@bert//models:derived_val",
) -> object:
    """Helper: invoke show with resolve_label_to_value returning resolved_value."""
    mock_ws = MagicMock()
    mock_ws.root_infos = {}
    mock_ws.expand_wildcard_label.return_value = [target]

    runner = CliRunner()
    with patch("mlody.cli.show.resolve_workspace") as mock_rw, \
         patch("mlody.cli.show.resolve_label_to_value") as mock_rlv:
        mock_rw.return_value = (mock_ws, None)
        mock_rlv.return_value = resolved_value
        return runner.invoke(
            cli,
            ["show", target],
            obj={"monorepo_root": tmp_path, "roots": None, "verbose": False},
        )


class TestShowPlainParquetValue:
    """Requirements: plain parquet-backed value preview and fallback behavior."""

    def test_plain_parquet_location_displays_preview(self, tmp_path: Path) -> None:
        """Scenario: path-backed value renders a preview through TabularSource."""
        parquet_path = tmp_path / "plain.parquet"
        pq.write_table(pa.table({"x": [1, 2, 3]}), parquet_path)

        result = _make_show_runner(
            tmp_path,
            _make_value_with_plain_location(str(parquet_path)),
            target="@bert//models:plain_table",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "3 rows" in result.output or "x" in result.output  # type: ignore[union-attr]

    def test_unreadable_plain_parquet_falls_back_to_dom_rendering(
        self, tmp_path: Path
    ) -> None:
        """Scenario: plain path-backed value falls back to DOM when parquet read fails."""
        parquet_path = tmp_path / "broken.parquet"

        result = _make_show_runner(
            tmp_path,
            _make_value_with_plain_location(str(parquet_path)),
            target="@bert//models:plain_table",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "plain_table" in result.output  # type: ignore[union-attr]


class TestShowRemoteTabularValue:
    """Requirements: remote csv/parquet values preview through the tabular path."""

    def test_show_remote_csv_value_displays_preview(
        self,
        tmp_path: Path,
        http_server: tuple[str, Path],
    ) -> None:
        base_url, root = http_server
        (root / "employees.csv").write_text("name,salary\nAlice,120000\nBob,90000\n")
        value = _make_value_with_remote_location(
            f"{base_url}/employees.csv",
            representation_name="csv",
            representation_attributes={
                "separator": ",",
                "header_required": True,
                "multifile": False,
            },
            name="raw_employees",
        )

        result = _make_show_runner(
            tmp_path,
            value,
            target="@bert//models:raw_employees",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "pyarrow.Table" in result.output  # type: ignore[union-attr]
        assert "Alice" in result.output  # type: ignore[union-attr]
        assert "salary" in result.output  # type: ignore[union-attr]
        assert "name, salary" not in result.output  # type: ignore[union-attr]

    def test_show_remote_parquet_value_displays_preview(
        self,
        tmp_path: Path,
        http_server: tuple[str, Path],
    ) -> None:
        base_url, root = http_server
        parquet_path = root / "employees.parquet"
        pq.write_table(pa.table({"name": ["Alice", "Bob"], "salary": [120000, 90000]}), parquet_path)
        value = _make_value_with_remote_location(
            f"{base_url}/employees.parquet",
            representation_name="parquet",
            representation_attributes={"multifile": False},
            name="raw_employees",
        )

        result = _make_show_runner(
            tmp_path,
            value,
            target="@bert//models:raw_employees",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "pyarrow.Table" in result.output  # type: ignore[union-attr]
        assert "Alice" in result.output  # type: ignore[union-attr]
        assert "salary" in result.output  # type: ignore[union-attr]
        assert "name, salary" not in result.output  # type: ignore[union-attr]

    def test_show_remote_unsupported_representation_falls_back(
        self,
        tmp_path: Path,
        http_server: tuple[str, Path],
    ) -> None:
        base_url, root = http_server
        (root / "data.json").write_text('{"hello": "world"}', encoding="utf-8")
        value = _make_value_with_remote_location(
            f"{base_url}/data.json",
            representation_name="json",
            representation_attributes={},
            name="remote_meta",
        )

        result = _make_show_runner(
            tmp_path,
            value,
            target="@bert//models:remote_meta",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "hello" in result.output  # type: ignore[union-attr]

    def test_show_remote_unsupported_representation_displays_asset_metadata(
        self,
        http_server: tuple[str, Path],
        tmp_path: Path,
    ) -> None:
        base_url, root = http_server
        (root / "data.json").write_text('{"hello": "world"}', encoding="utf-8")
        value = _make_value_with_remote_location(
            f"{base_url}/data.json",
            representation_name="json",
            representation_attributes={},
            name="remote_meta",
        )

        result = _make_show_runner(
            tmp_path,
            value,
            target="@bert//models:remote_meta",
        )

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert "hello" in result.output  # type: ignore[union-attr]
        assert "world" in result.output  # type: ignore[union-attr]

    def test_show_source_backed_local_csv_value_materializes_once_and_reuses_cache(
        self,
        tmp_path: Path,
    ) -> None:
        staged_path = tmp_path / "employees.csv"
        staged_path.write_text("name,salary\nAlice,120000\nBob,90000\n")
        destination_path = tmp_path / "artifacts" / "employees.csv"
        value = _make_value_with_source_backed_local_location(
            str(destination_path),
            source_uri="https://example.com/employees.csv",
            name="raw_employees_local",
        )

        with patch("mlody.core.assets.http_asset.HttpAssetSource.materialize") as mock_materialize:
            mock_materialize.return_value = _remote_asset(
                staged_path,
                uri="https://example.com/employees.csv",
                content_hash="abc123",
            )
            first = _make_show_runner(
                tmp_path,
                value,
                target="@bert//models:raw_employees_local",
            )
            second = _make_show_runner(
                tmp_path,
                value,
                target="@bert//models:raw_employees_local",
            )

        assert first.exit_code == 0  # type: ignore[union-attr]
        assert second.exit_code == 0  # type: ignore[union-attr]
        assert "Alice" in first.output  # type: ignore[union-attr]
        assert "salary" in second.output  # type: ignore[union-attr]
        assert destination_path.exists()
        assert destination_path.read_text() == staged_path.read_text()
        assert mock_materialize.call_count == 1


class TestShowDerivedValue:
    """Requirements: derived value materialisation in mlody show."""

    def test_cache_miss_materialises_and_displays_table(self, tmp_path: Path) -> None:
        """Scenario: derived value — cache miss materialises and displays table."""
        source_path = tmp_path / "source.parquet"
        output_path = str(tmp_path / "output.parquet")
        pq.write_table(pa.table({"x": [1, 2, 3], "y": [4, 5, 6]}), source_path)
        value = _make_value_with_derived_location(
            output_path,
            source_paths=[str(source_path)],
        )
        result = _invoke_show_with_derived(tmp_path, value)

        assert result.exit_code == 0  # type: ignore[union-attr]
        assert Path(output_path).exists()
        assert "3 rows" in result.output or "x" in result.output  # type: ignore[union-attr]

    def test_cache_hit_reuses_file_without_reexecution(self, tmp_path: Path) -> None:
        """Scenario: derived value — cache hit reuses file without re-execution."""
        output_path = str(tmp_path / "cached.parquet")
        # Pre-create the cached file
        cached_table = pa.table({"a": [10, 20], "b": [30, 40]})
        pq.write_table(cached_table, output_path)

        value = _make_value_with_derived_location(
            output_path,
            source_paths=[str(tmp_path / "source.parquet")],
        )
        result = _invoke_show_with_derived(tmp_path, value)

        assert result.exit_code == 0  # type: ignore[union-attr]

    def test_shape_error_exits_1_with_red_message(self, tmp_path: Path) -> None:
        """Requirement: DerivedValueShapeError displayed as a resolution error."""
        source_path = tmp_path / "source.parquet"
        output_path = str(tmp_path / "output.parquet")
        pq.write_table(pa.table({"x": [1, 2, 3]}), source_path)
        value = _make_value_with_derived_location(
            output_path,
            sql_fragment="SELECT COUNT(*)",
            source_paths=[str(source_path)],
        )
        result = _invoke_show_with_derived(tmp_path, value)

        assert result.exit_code == 1  # type: ignore[union-attr]
        output_and_err = (result.output or "") + (result.stderr or "")
        assert "SELECT COUNT(*)" in output_and_err or "scalar" in output_and_err.lower()

    def test_sql_error_exits_1_with_message_no_traceback(self, tmp_path: Path) -> None:
        """Requirement: MlodyQueryError during materialisation displayed as error."""
        source_path = tmp_path / "source.parquet"
        output_path = str(tmp_path / "output.parquet")
        pq.write_table(pa.table({"x": [1, 2, 3]}), source_path)
        value = _make_value_with_derived_location(
            output_path,
            sql_fragment="BAD SQL",
            source_paths=[str(source_path)],
        )
        result = _invoke_show_with_derived(tmp_path, value)

        assert result.exit_code == 1  # type: ignore[union-attr]
        output_and_err = (result.output or "") + (result.stderr or "")
        # Must contain the error message
        assert "BAD SQL" in output_and_err or "syntax error" in output_and_err
        # Must NOT contain a Python traceback
        assert "Traceback" not in result.output  # type: ignore[union-attr]
