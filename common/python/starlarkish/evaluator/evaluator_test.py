from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from common.python.starlarkish.evaluator.evaluator import Evaluator


@pytest.fixture
def project_root(fs: FakeFilesystem) -> Path:  # fs is the pyfakefs fixture
    """Set up a fake filesystem for tests and return the root path."""
    root_path = Path("/project")
    root_path.mkdir()

    # Create some virtual files
    _ = fs.create_file(
        "/project/lib.mlody",
        contents="""
MY_CONSTANT = "hello from lib"

def my_func():
    return "data from func"
""",
    )

    _ = fs.create_file(
        "/project/subdir/helper.mlody",
        contents="""
HELPER_VAR = "helper"
""",
    )

    _ = fs.create_file(
        "/project/entry.mlody",
        contents="""
load("//lib.mlody", "MY_CONSTANT")
load(":subdir/helper.mlody", "HELPER_VAR")

builtins.register("root", struct(name="entry_point", const=MY_CONSTANT, helper=HELPER_VAR))
""",
    )

    _ = fs.create_file(
        "/project/bad_sandbox.mlody",
        contents="""
open("/etc/passwd")
""",
    )
    return root_path

def test_simple_execution_and_registration(fs: FakeFilesystem, project_root: Path) -> None:
    """Test basic script execution and object registration."""
    _ = fs.create_file(
        "/project/simple.mlody",
        contents="""
builtins.register("root", struct(name="simple_root", value=42))
""",
    )
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "simple.mlody")

    assert "simple_root" in evaluator.roots
    assert evaluator.roots["simple_root"].name == "simple_root"
    assert evaluator.roots["simple_root"].value == 42

def test_load_and_registration(project_root: Path) -> None:
    """Test loading from other files and registering combined results."""
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "entry.mlody")

    assert "entry_point" in evaluator.roots
    root_obj = evaluator.roots["entry_point"]
    assert root_obj.name == "entry_point"
    assert root_obj.const == "hello from lib"
    assert root_obj.helper == "helper"

def test_sandboxing(project_root: Path) -> None:
    """Test that scripts cannot access disallowed builtins."""
    evaluator = Evaluator(project_root)
    with pytest.raises(NameError, match="name 'open' is not defined"):
        evaluator.eval_file(project_root / "bad_sandbox.mlody")

def test_load_all_symbols(fs: FakeFilesystem, project_root: Path) -> None:
    """Test load() without explicit symbols, importing all public names."""
    _ = fs.create_file(
        "/project/load_all.mlody",
        contents="""
load("//lib.mlody")
result = my_func()
builtins.register("root", struct(name="loaded_all", const=MY_CONSTANT, func_result=result))
""",
    )
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "load_all.mlody")

    assert "loaded_all" in evaluator.roots
    root_obj = evaluator.roots["loaded_all"]
    assert root_obj.const == "hello from lib"
    assert root_obj.func_result == "data from func"

def test_default_print_fn_writes_to_stdout(
    fs: FakeFilesystem, project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Requirement: print() in sandbox writes to stdout by default.

    This is the expected CLI behaviour — users can call print() in .mlody
    scripts and see output on the terminal.
    """
    fs.create_file("/project/printer.mlody", contents='print("hello from mlody")\n')
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "printer.mlody")

    captured = capsys.readouterr()
    assert "hello from mlody" in captured.out


def test_custom_print_fn_replaces_sandbox_print(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """Requirement: custom print_fn is called instead of builtins.print.

    The LSP server passes a no-op here so that sandbox print() calls do not
    corrupt the stdout JSON-RPC transport.
    """
    mock_print = MagicMock()
    fs.create_file("/project/printer.mlody", contents='print("intercepted")\n')
    evaluator = Evaluator(project_root, print_fn=mock_print)
    evaluator.eval_file(project_root / "printer.mlody")

    mock_print.assert_called_once_with("intercepted")


def test_caching_of_loaded_files(fs: FakeFilesystem, project_root: Path) -> None:
    """Test that files are only executed once."""
    # A file that loads another file.
    _ = fs.create_file(
        "/project/loader.mlody",
        contents="load('//lib.mlody', 'MY_CONSTANT')",
    )
    evaluator = Evaluator(project_root)

    with patch('builtins.exec') as mock_exec:
        # First execution of lib.mlody
        evaluator.eval_file(project_root / "lib.mlody")
        assert mock_exec.call_count == 1, "exec should be called for lib.mlody"

        # Execute a file that loads the same library
        evaluator.eval_file(project_root / "loader.mlody")
        # exec is called for loader.mlody, but not for lib.mlody because it's cached
        assert mock_exec.call_count == 2, "exec should be called for loader.mlody"

        # Re-executing loader.mlody should not trigger any more exec calls
        evaluator.eval_file(project_root / "loader.mlody")
        assert mock_exec.call_count == 2, "exec should not be called again for cached file"
