"""In-memory filesystem for testing the Starlarkish evaluator."""
import io
import os
from pathlib import Path
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
        self.resolve_patcher = mock.patch("pathlib.Path.resolve", side_effect=self._mock_resolve)

    def __enter__(self) -> Path:
        self.open_patcher.start()
        self.resolve_patcher.start()
        return self.root

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.open_patcher.stop()
        self.resolve_patcher.stop()

    def _mock_open(self, file: str | Path, mode: str = 'r', *args: Any, **kwargs: Any) -> io.StringIO:
        # Path().resolve() will call our mocked _mock_resolve
        file_path = str(Path(file).resolve())
        if file_path in self.files:
            return io.StringIO(self.files[file_path])
        raise FileNotFoundError(f"[Errno 2] No such file or directory: '{file}'")

    @staticmethod
    def _mock_resolve(path_instance: Path) -> Path:
        """A mock for Path.resolve that handles . and .. correctly."""
        return Path(os.path.normpath(str(path_instance)))
