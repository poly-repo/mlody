"""Tests for mlody.cli.show — show subcommand and show_fn."""

from __future__ import annotations

import functools
import http.server
import logging
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

import networkx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from common.python.starlarkish.core.struct import Struct, struct

import mlody.cli.show
from mlody.cli.dag_render import DagSelectionResult
from mlody.cli.main import cli
from mlody.cli.show import show_fn
from mlody.core.label import parse_label as _parse_label
from mlody.resolver.errors import UnknownRefError
from mlody.resolver.label_value import (
    MlodyActionValue,
    MlodyFolderValue,
    MlodySourceValue,
    MlodyTaskValue,
    MlodyUnresolvedValue,
    MlodyValueValue,
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

    def test_cwd_target_with_legacy_workspace_injection(self) -> None:
        # Existing tests inject workspace — this legacy path must still work
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output


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
# CLI show command — output rendering
# ---------------------------------------------------------------------------


class TestShowCommandOutput:
    """Requirement: Resolve and display target values."""

    def test_primitive_value_displayed_as_plain_string(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = 0.001
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:lr"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "0.001" in result.output

    def test_struct_value_displayed_via_pretty_repr(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = struct(name="bert", lr=0.001)
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:config"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 0
        assert "bert" in result.output
        assert "0.001" in result.output

    def test_multiple_targets_displayed_in_order(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, "adam"]
        ws.root_infos = {}

        runner = CliRunner()
        result = runner.invoke(
            cli, ["show", "@bert//models:lr", "@bert//models:opt"], obj={"workspace": ws, "verbose": False}
        )

        assert result.exit_code == 0
        lr_pos = result.output.index("0.001")
        opt_pos = result.output.index("adam")
        assert lr_pos < opt_pos


class TestShowCommandDagPlan:
    """Requirement: output labels render the same DAG table used by dag."""

    def test_output_label_renders_pruned_dag_table(self) -> None:
        ws = MagicMock()
        ws.resolve.return_value = "model-value"
        ws.root_infos = {}

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


# ---------------------------------------------------------------------------
# CLI show command — error handling
# ---------------------------------------------------------------------------


class TestShowCommandErrors:
    """Requirement: Clear error messages for resolution failures."""

    def test_missing_root_shows_error_with_available_roots(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = KeyError("NONEXISTENT")
        ws.root_infos = {"lexica": MagicMock(), "common": MagicMock()}

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

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "@bert//models:bad_field"], obj={"workspace": ws, "verbose": False})

        assert result.exit_code == 1
        assert "bad_field" in result.stderr

    def test_partial_failure_shows_successes_and_errors(self) -> None:
        ws = MagicMock()
        ws.resolve.side_effect = [0.001, KeyError("MISSING")]
        ws.root_infos = {}

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
            inputs=Struct(),
            config=Struct(),
            outputs=Struct(
                model=Struct(
                    name="model",
                    type=_make_type_struct("nothing", root_kind="nothing"),
                    source=Struct(type="inline"),
                    default=None,
                ),
                committoid=Struct(
                    name="committoid",
                    type=_make_type_struct("nothing", root_kind="nothing"),
                    source=Struct(type="inline"),
                    default=None,
                ),
                releases=Struct(
                    name="releases",
                    type=vector_string,
                    source=Struct(type="inline"),
                    default=None,
                ),
            ),
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
        assert "Alice" in result.output  # type: ignore[union-attr]
        assert "salary" in result.output  # type: ignore[union-attr]

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
        assert "Alice" in result.output  # type: ignore[union-attr]
        assert "salary" in result.output  # type: ignore[union-attr]

    def test_show_remote_unsupported_representation_falls_back(
        self,
        tmp_path: Path,
    ) -> None:
        value = _make_value_with_remote_location(
            "https://example.com/data.json",
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
        assert "remote_meta" in result.output  # type: ignore[union-attr]


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
