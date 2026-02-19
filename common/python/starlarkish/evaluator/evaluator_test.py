from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from common.python.starlarkish.evaluator import evaluator as evaluator_module
from common.python.starlarkish.evaluator.evaluator import Evaluator
from common.python.starlarkish.evaluator.testing import InMemoryFS


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
    assert evaluator.roots["simple_root"].value == 42  # type: ignore[attr-defined]


def test_load_and_registration(fs: FakeFilesystem, project_root: Path) -> None:
    """Test loading from other files and registering combined results."""
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "entry.mlody")

    assert "entry_point" in evaluator.roots
    root_obj = evaluator.roots["entry_point"]
    assert root_obj.name == "entry_point"
    assert root_obj.const == "hello from lib"  # type: ignore[attr-defined]
    assert root_obj.helper == "helper"  # type: ignore[attr-defined]


def test_sandboxing(fs: FakeFilesystem, project_root: Path) -> None:
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
    assert root_obj.const == "hello from lib"  # type: ignore[attr-defined]
    assert root_obj.func_result == "data from func"  # type: ignore[attr-defined]


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
    """Files are executed at most once; repeated loads return cached globals.

    Tests requirement: Caching test validates observable behaviour.
    Spies on _execute_file at the instance level so only calls on this
    evaluator are counted (no global builtins.exec patch).
    """
    _ = fs.create_file(
        "/project/loader.mlody",
        contents="load('//lib.mlody', 'MY_CONSTANT')",
    )
    ev = Evaluator(project_root)

    with patch.object(ev, "_execute_file", wraps=ev._execute_file) as spy:
        # First eval: loader.mlody executed, which loads lib.mlody (2 calls total)
        ev.eval_file(project_root / "loader.mlody")
        calls_after_first = spy.call_count
        assert calls_after_first == 2

        # Second eval: loader.mlody is cached; returns before running load().
        # Only one additional _execute_file call (the early-return cache check).
        ev.eval_file(project_root / "loader.mlody")
        assert spy.call_count == calls_after_first + 1

    # Each unique file ends up in loaded_files exactly once
    assert len(ev.loaded_files) == 2


def test_register_unknown_kind_raises(fs: FakeFilesystem, project_root: Path) -> None:
    """Registering with an unknown kind raises ValueError.

    Tests requirement: Registration kind validation
    (scenario: Unknown kind rejected).
    """
    _ = fs.create_file(
        "/project/bad_register.mlody",
        contents="""
builtins.register("target", struct(name="oops"))
""",
    )
    evaluator = Evaluator(project_root)
    with pytest.raises(ValueError, match="target"):
        evaluator.eval_file(project_root / "bad_register.mlody")


def test_circular_import_raises(fs: FakeFilesystem, project_root: Path) -> None:
    """Mutually-loading files raise ImportError with the full cycle path.

    Tests requirement: Evaluator test coverage for error paths
    (scenario: Circular import raises ImportError).
    """
    _ = fs.create_file("/project/a.mlody", contents='load("//b.mlody")\n')
    _ = fs.create_file("/project/b.mlody", contents='load("//a.mlody")\n')

    evaluator = Evaluator(project_root)
    with pytest.raises(ImportError, match="Circular import detected"):
        evaluator.eval_file(project_root / "a.mlody")


def test_load_nonexistent_file_raises(fs: FakeFilesystem, project_root: Path) -> None:
    """Loading a missing file raises FileNotFoundError.

    Tests requirement: Evaluator test coverage for error paths
    (scenario: Load of nonexistent file raises FileNotFoundError).
    """
    _ = fs.create_file(
        "/project/missing.mlody",
        contents='load("//nonexistent.mlody")\n',
    )
    evaluator = Evaluator(project_root)
    with pytest.raises(FileNotFoundError):
        evaluator.eval_file(project_root / "missing.mlody")


def test_load_invalid_symbol_raises(fs: FakeFilesystem, project_root: Path) -> None:
    """Loading a symbol that does not exist in the target raises NameError.

    Tests requirement: Evaluator test coverage for error paths
    (scenario: Load of invalid symbol raises NameError).
    """
    _ = fs.create_file(
        "/project/bad_symbol.mlody",
        contents='load("//lib.mlody", "NO_SUCH_SYMBOL")\n',
    )
    evaluator = Evaluator(project_root)
    with pytest.raises(NameError, match="NO_SUCH_SYMBOL"):
        evaluator.eval_file(project_root / "bad_symbol.mlody")


def test_init_files_preloaded(fs: FakeFilesystem, project_root: Path) -> None:
    """Files passed as init_files are executed before eval_file() is called.

    Tests requirement: Evaluator test coverage for error paths
    (scenario: init_files are pre-executed).
    """
    _ = fs.create_file(
        "/project/init.mlody",
        contents="""
builtins.register("root", struct(name="preloaded", value=99))
""",
    )
    evaluator = Evaluator(project_root, init_files=[project_root / "init.mlody"])

    # Root must be registered without any eval_file() call
    assert "preloaded" in evaluator.roots
    assert evaluator.roots["preloaded"].value == 99  # type: ignore[attr-defined]


def test_inmemoryfs_roots_smoketest() -> None:
    """End-to-end smoke test: Evaluator + InMemoryFS registers a root correctly.

    Tests requirement: InMemoryFS end-to-end smoketest with roots.mlody
    (scenario: Evaluator registers root via InMemoryFS).
    """
    files = {
        "smoke.mlody": (
            'builtins.register("root", '
            'struct(name="smoke_root", path="//smoke", description="test"))\n'
        ),
    }
    with InMemoryFS(files) as root:
        ev = Evaluator(root)
        ev.eval_file(root / "smoke.mlody")

    assert "smoke_root" in ev.roots
    assert ev.roots["smoke_root"].name == "smoke_root"
    assert ev.roots["smoke_root"].path == "//smoke"  # type: ignore[attr-defined]
