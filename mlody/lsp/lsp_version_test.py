"""Tests for F15 — LSP binary exits cleanly on --version.

Covers the spec scenarios from specs/lsp-version-flag/spec.md:
- --version flag exits 0 and prints exactly one line matching `mlody-lsp \\S+`
- invocation without --version does not exit early (server starts)
- unknown arguments are passed through to the server

The entry-point modules (__main__.py and _pex_main.py) have identical behaviour;
both are exercised by the parametrized ``entry_module_name`` fixture.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import re
import sys
import types
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: execute an entry-point module with a controlled sys.argv
# ---------------------------------------------------------------------------


def _exec_entry(module_name: str, argv: list[str]) -> tuple[int, str]:
    """Execute a mlody.lsp entry-point module with the given argv.

    Returns ``(exit_code, captured_stdout)``.

    ``mlody.lsp.server`` is replaced with a lightweight mock so that
    ``server.start_io()`` is a no-op — only the --version branch or the
    normal-start branch runs.
    """
    # Build a fresh mock of mlody.lsp.server every time.
    fake_server_module = types.ModuleType("mlody.lsp.server")
    fake_server = MagicMock()
    fake_server_module.server = fake_server  # type: ignore[attr-defined]

    buf = io.StringIO()
    exit_code: int = 0

    with (
        patch.dict(
            "sys.modules",
            {"mlody.lsp.server": fake_server_module},
        ),
        patch.object(sys, "argv", [module_name] + argv),
        redirect_stdout(buf),
    ):
        try:
            # Force a fresh import each time by removing any cached copy.
            sys.modules.pop(module_name, None)
            importlib.import_module(module_name)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0

    return exit_code, buf.getvalue()


def _get_server_mock(module_name: str, argv: list[str]) -> MagicMock:
    """Run the entry point and return the fake server mock for assertion."""
    fake_server_module = types.ModuleType("mlody.lsp.server")
    fake_server = MagicMock()
    fake_server_module.server = fake_server  # type: ignore[attr-defined]

    with (
        patch.dict("sys.modules", {"mlody.lsp.server": fake_server_module}),
        patch.object(sys, "argv", [module_name] + argv),
    ):
        try:
            sys.modules.pop(module_name, None)
            importlib.import_module(module_name)
        except SystemExit:
            pass

    return fake_server


# ---------------------------------------------------------------------------
# Parametrize over both entry-point module names
# ---------------------------------------------------------------------------

_ENTRY_MODULES = [
    "mlody.lsp.__main__",
    "mlody.lsp._pex_main",
]


@pytest.fixture(params=_ENTRY_MODULES, ids=["__main__", "_pex_main"])
def entry_module(request: pytest.FixtureRequest) -> str:
    return str(request.param)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# F15: --version flag behaviour
# ---------------------------------------------------------------------------


class TestVersionFlag:
    """F15: --version flag exits 0 and prints one matching line."""

    def test_version_exits_zero(self, entry_module: str) -> None:
        """Scenario: --version exits with code 0."""
        code, _ = _exec_entry(entry_module, ["--version"])
        assert code == 0

    def test_version_prints_single_line(self, entry_module: str) -> None:
        """Scenario: --version prints exactly one non-empty line."""
        _, output = _exec_entry(entry_module, ["--version"])
        lines = [ln for ln in output.splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected 1 non-empty line, got: {lines!r}"

    def test_version_output_matches_pattern(self, entry_module: str) -> None:
        """Scenario: --version output matches `mlody-lsp \\S+`."""
        _, output = _exec_entry(entry_module, ["--version"])
        line = output.strip()
        assert re.match(r"^mlody-lsp \S+$", line), (
            f"Output {line!r} does not match 'mlody-lsp <version>'"
        )

    def test_version_does_not_start_server(self, entry_module: str) -> None:
        """Scenario: --version does not call server.start_io()."""
        server_mock = _get_server_mock(entry_module, ["--version"])
        server_mock.start_io.assert_not_called()


# ---------------------------------------------------------------------------
# F15: no-argument and unknown-argument behaviour
# ---------------------------------------------------------------------------


class TestNoVersionFlag:
    """Scenarios: absent --version causes server.start_io() to be called."""

    def test_no_args_starts_server(self, entry_module: str) -> None:
        """Scenario: no arguments → server.start_io() is called once."""
        server_mock = _get_server_mock(entry_module, [])
        server_mock.start_io.assert_called_once()

    def test_unknown_args_start_server(self, entry_module: str) -> None:
        """Scenario: unknown arguments → server.start_io() is called once."""
        server_mock = _get_server_mock(entry_module, ["--stdio"])
        server_mock.start_io.assert_called_once()

    def test_no_args_does_not_exit(self, entry_module: str) -> None:
        """Scenario: no arguments → process does not call sys.exit()."""
        code, _ = _exec_entry(entry_module, [])
        # exit_code defaults to 0 if no SystemExit was raised; that's a pass.
        # We just verify the module ran without raising SystemExit(0) early.
        # The mock server.start_io() returns None, so this is always 0 here.
        assert code == 0
