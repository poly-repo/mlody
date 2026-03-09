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

    assert "simple:simple_root" in evaluator.roots
    assert evaluator.roots["simple:simple_root"].name == "simple_root"
    assert evaluator.roots["simple:simple_root"].value == 42  # type: ignore[attr-defined]


def test_load_and_registration(fs: FakeFilesystem, project_root: Path) -> None:
    """Test loading from other files and registering combined results."""
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "entry.mlody")

    assert "entry:entry_point" in evaluator.roots
    root_obj = evaluator.roots["entry:entry_point"]
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

    assert "load_all:loaded_all" in evaluator.roots
    root_obj = evaluator.roots["load_all:loaded_all"]
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
    assert "init:preloaded" in evaluator.roots
    assert evaluator.roots["init:preloaded"].value == 99  # type: ignore[attr-defined]


def test_load_at_root_with_package(fs: FakeFilesystem, project_root: Path) -> None:
    """load("@ROOT//package:file.mlody") resolves via the named root's path field."""
    fs.create_file(
        "/project/mlody/roots.mlody",
        contents="""
builtins.register("root", struct(name="myroot", path="//mlody/lib"))
""",
    )
    fs.create_file(
        "/project/mlody/lib/models/bert.mlody",
        contents='BERT = "bert-base"\n',
    )
    fs.create_file(
        "/project/mlody/consumer.mlody",
        contents='load("@myroot//models:bert.mlody", "BERT")\nbuiltins.register("root", struct(name="consumer", value=BERT))\n',
    )
    ev = Evaluator(project_root)
    ev.eval_file(project_root / "mlody/roots.mlody")
    ev.eval_file(project_root / "mlody/consumer.mlody")

    assert ev.roots["mlody/consumer:consumer"].value == "bert-base"  # type: ignore[attr-defined]


def test_load_at_root_no_package(fs: FakeFilesystem, project_root: Path) -> None:
    """load("@ROOT//:file.mlody") resolves to the root directory itself."""
    fs.create_file(
        "/project/lib_root.mlody",
        contents='builtins.register("root", struct(name="base", path="//mlody/lib"))\n',
    )
    fs.create_file(
        "/project/mlody/lib/helpers.mlody",
        contents='HELPER = "ok"\n',
    )
    fs.create_file(
        "/project/mlody/consumer.mlody",
        contents='load("@base//:helpers.mlody", "HELPER")\nbuiltins.register("root", struct(name="r", value=HELPER))\n',
    )
    ev = Evaluator(project_root)
    ev.eval_file(project_root / "lib_root.mlody")
    ev.eval_file(project_root / "mlody/consumer.mlody")

    assert ev.roots["mlody/consumer:r"].value == "ok"  # type: ignore[attr-defined]


def test_load_at_root_idempotent_with_direct_load(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """A file loaded via @ROOT// and then again directly is executed only once.

    This mirrors what happens when the Workspace Phase 2 glob re-discovers a
    file that was already pulled in via a load("@ROOT//...") call.
    """
    from unittest.mock import patch

    fs.create_file(
        "/project/mlody/roots.mlody",
        contents='builtins.register("root", struct(name="r", path="//mlody/lib"))\n',
    )
    fs.create_file("/project/mlody/lib/shared.mlody", contents='SHARED = 1\n')
    fs.create_file(
        "/project/mlody/consumer.mlody",
        contents='load("@r//:shared.mlody", "SHARED")\nbuiltins.register("root", struct(name="c", value=SHARED))\n',
    )
    ev = Evaluator(project_root)
    ev.eval_file(project_root / "mlody/roots.mlody")

    with patch.object(ev, "_execute_file", wraps=ev._execute_file) as spy:
        # Loads consumer.mlody (1) which pulls shared.mlody via @r// (2)
        ev.eval_file(project_root / "mlody/consumer.mlody")
        calls_after_first = spy.call_count
        assert calls_after_first == 2

        # Directly loading shared.mlody again — must be a cache hit, no re-execution
        ev.eval_file(project_root / "mlody/lib/shared.mlody")
        assert spy.call_count == calls_after_first + 1  # one extra call, returns immediately

    assert len([f for f in ev.loaded_files if "shared" in str(f)]) == 1


def test_load_at_root_unknown_root_raises(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """Referencing an unregistered @ROOT raises NameError."""
    fs.create_file(
        "/project/bad.mlody",
        contents='load("@ghost//pkg:file.mlody")\n',
    )
    ev = Evaluator(project_root)
    with pytest.raises(NameError, match="ghost"):
        ev.eval_file(project_root / "bad.mlody")


def test_load_at_root_missing_colon_raises(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """@ROOT// path without ':' raises ValueError."""
    fs.create_file(
        "/project/bad.mlody",
        contents='load("@myroot//pkg/file.mlody")\n',
    )
    ev = Evaluator(project_root)
    with pytest.raises(ValueError, match="':'"):
        ev.eval_file(project_root / "bad.mlody")


def test_register_receives_ctx(fs: FakeFilesystem, project_root: Path) -> None:
    """_register is called with the ctx of the file that triggered the registration.

    The ctx is bound transparently via functools.partial — .mlody scripts still
    call builtins.register(kind, thing) with two arguments.
    """
    from common.python.starlarkish.core.struct import Struct as StructType

    captured: list[StructType] = []

    class CapturingEvaluator(Evaluator):
        def _register(self, kind: str, thing: object, ctx: StructType) -> None:  # type: ignore[override]
            captured.append(ctx)
            super()._register(kind, thing, ctx)  # type: ignore[arg-type]

    fs.create_file(
        "/project/reg.mlody",
        contents='builtins.register("root", struct(name="r", value=1))\n',
    )
    ev = CapturingEvaluator(project_root)
    ev.eval_file(project_root / "reg.mlody")

    assert len(captured) == 1
    assert captured[0].directory == project_root  # type: ignore[attr-defined]


def test_register_ctx_directory_reflects_caller_file(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """ctx.directory in _register is the calling file's directory, not the helper's.

    A loaded helper function that calls builtins.register must not pin
    ctx.directory to the helper's own directory.  The closure re-reads
    _eval_stack at call time so the caller's directory is always used.
    """
    from common.python.starlarkish.core.struct import Struct as StructType

    captured: list[StructType] = []

    class CapturingEvaluator(Evaluator):
        def _register(self, kind: str, thing: object, ctx: StructType) -> None:  # type: ignore[override]
            captured.append(ctx)
            super()._register(kind, thing, ctx)  # type: ignore[arg-type]

    # Helper lives in a dedicated subdirectory — if the bug were present,
    # ctx.directory would be /project/helpers instead of /project.
    fs.create_file(
        "/project/helpers/reg_helper.mlody",
        contents=(
            "def do_register(name):\n"
            '    builtins.register("root", struct(name=name, value=1))\n'
        ),
    )
    fs.create_file(
        "/project/main.mlody",
        contents=(
            'load(":helpers/reg_helper.mlody", "do_register")\n'
            'do_register("r")\n'
        ),
    )
    ev = CapturingEvaluator(project_root)
    ev.eval_file(project_root / "main.mlody")

    assert len(captured) == 1
    # Must be the calling file's directory (project root), not the helper's (subdir)
    assert captured[0].directory == project_root  # type: ignore[attr-defined]


def test_load_after_code_raises(fs: FakeFilesystem, project_root: Path) -> None:
    """load() after a non-load statement raises SyntaxError.

    Starlark requires all load() calls to appear before any other code.
    """
    fs.create_file(
        "/project/bad_load_order.mlody",
        contents="X = 5\nload('//lib.mlody')\n",
    )
    evaluator = Evaluator(project_root)
    with pytest.raises(SyntaxError, match="load\\(\\).*must appear before"):
        evaluator.eval_file(project_root / "bad_load_order.mlody")


def test_load_after_other_load_then_code_then_load_raises(
    fs: FakeFilesystem, project_root: Path
) -> None:
    """load() after valid loads followed by other code raises SyntaxError.

    Even when valid loads precede the bad one, the violation must be caught.
    """
    fs.create_file(
        "/project/mixed_load.mlody",
        contents=(
            "load('//lib.mlody', 'MY_CONSTANT')\n"
            "X = 5\n"
            "load('//subdir/helper.mlody')\n"
        ),
    )
    evaluator = Evaluator(project_root)
    with pytest.raises(SyntaxError, match="load\\(\\).*must appear before"):
        evaluator.eval_file(project_root / "mixed_load.mlody")


def test_docstring_then_load_is_allowed(fs: FakeFilesystem, project_root: Path) -> None:
    """A module docstring followed by load() is valid and must not raise."""
    fs.create_file(
        "/project/docstring_load.mlody",
        contents='"""Module docstring."""\nload("//lib.mlody", "MY_CONSTANT")\n',
    )
    evaluator = Evaluator(project_root)
    evaluator.eval_file(project_root / "docstring_load.mlody")
    # No error raised; MY_CONSTANT is now in the loaded file's globals.


def test_primitive_types_pre_registered(fs: FakeFilesystem) -> None:
    """Evaluator pre-registers primitive type sentinels on construction."""
    root = Path("/project")
    root.mkdir()
    ev = Evaluator(root)
    assert "integer" in ev.types
    assert "string" in ev.types
    assert "bool" in ev.types
    assert "float" in ev.types


def test_lookup_accessible_from_script(fs: FakeFilesystem) -> None:
    """Scripts can call builtins.lookup() to access registered types."""
    root = Path("/project")
    root.mkdir()
    fs.create_file(
        "/project/test.mlody",
        contents=(
            'result_name = builtins.lookup("type", "integer").name\n'
            'builtins.register("root", struct(name="r", type_name=result_name))\n'
        ),
    )
    ev = Evaluator(root)
    ev.eval_file(root / "test.mlody")
    assert ev.roots["test:r"].type_name == "integer"  # type: ignore[attr-defined]


def test_lookup_unknown_kind_raises_value_error(fs: FakeFilesystem) -> None:
    """builtins.lookup with an unknown kind raises ValueError."""
    root = Path("/project")
    root.mkdir()
    fs.create_file("/project/bad.mlody", contents='builtins.lookup("widget", "foo")\n')
    ev = Evaluator(root)
    with pytest.raises(ValueError, match="Unknown lookup kind"):
        ev.eval_file(root / "bad.mlody")


def test_lookup_unknown_name_raises_name_error(fs: FakeFilesystem) -> None:
    """builtins.lookup with an unknown name raises NameError."""
    root = Path("/project")
    root.mkdir()
    fs.create_file("/project/bad.mlody", contents='builtins.lookup("type", "no_such_type")\n')
    ev = Evaluator(root)
    with pytest.raises(NameError, match="no_such_type"):
        ev.eval_file(root / "bad.mlody")


def test_lookup_returns_registered_type(fs: FakeFilesystem) -> None:
    """Register a type then look it up — the round-trip returns the same object."""
    root = Path("/project")
    root.mkdir()
    fs.create_file(
        "/project/test.mlody",
        contents=(
            'builtins.register("type", struct(name="my_type", kind="type"))\n'
            'found = builtins.lookup("type", "my_type")\n'
            'builtins.register("root", struct(name="r", found_name=found.name))\n'
        ),
    )
    ev = Evaluator(root)
    ev.eval_file(root / "test.mlody")
    assert ev.roots["test:r"].found_name == "my_type"  # type: ignore[attr-defined]


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

    assert "smoke:smoke_root" in ev.roots
    assert ev.roots["smoke:smoke_root"].name == "smoke_root"
    assert ev.roots["smoke:smoke_root"].path == "//smoke"  # type: ignore[attr-defined]
