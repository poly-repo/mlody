"""In-memory filesystem utilities for testing the Starlarkish evaluator.

``InMemoryFS`` is a higher-level multi-file ``.mlody`` test utility.  It lets
you write evaluator tests that need several ``.mlody`` files without touching
the real filesystem, without setting up ``pyfakefs`` manually for each test.

Typical usage::

    files = {
        "lib.mlody": "MY_CONST = 42",
        "entry.mlody": 'load("//lib.mlody", "MY_CONST")',
    }
    with InMemoryFS(files) as root:
        evaluator = Evaluator(root)
        evaluator.eval_file(root / "entry.mlody")
        assert evaluator.roots["something"].value == 42
"""
import io
import os
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest import mock


class InMemoryFS:
    """
    A context manager to simulate an in-memory filesystem for testing.

    Usage:
        files = {"file1.txt": "content1", "subdir/file2.txt": "content2"}
        with InMemoryFS(files) as root:
            # Inside this block, open() and Path.resolve() are mocked.
            # `root` is the pathlib.Path object for the virtual root dir.
            content = (root / "file1.txt").read_text()
    """
    def __init__(self, files: dict[str, str], root: Path | str = "/project"):
        self.root = Path(root)
        self.files: dict[str, str] = {
            str(self.root / p): c for p, c in files.items()
        }
        self.open_patcher = mock.patch("builtins.open", side_effect=self._mock_open)
        # Use `new=` so the replacement is treated as a descriptor: Python binds
        # the path instance as the first argument, matching the original signature.
        self.resolve_patcher = mock.patch.object(Path, "resolve", new=InMemoryFS._mock_resolve)

    def __enter__(self) -> Path:
        self.open_patcher.start()
        self.resolve_patcher.start()
        return self.root

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.open_patcher.stop()
        self.resolve_patcher.stop()

    def _mock_open(self, file: str | Path, mode: str = 'r', *args: Any, **kwargs: Any) -> io.StringIO | io.BytesIO:
        # Path().resolve() will call our mocked _mock_resolve
        file_path = str(Path(file).resolve())
        if file_path not in self.files:
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{file}'")
        content = self.files[file_path]
        if 'b' in mode:
            return io.BytesIO(content.encode('utf-8'))
        return io.StringIO(content)

    @staticmethod
    def _mock_resolve(path_instance: Path) -> Path:
        """A mock for Path.resolve that handles . and .. correctly."""
        return Path(os.path.normpath(str(path_instance)))
