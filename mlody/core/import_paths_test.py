"""Regression tests for canonical import paths across the repo."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest


def test_mlody_common_import_path_is_available() -> None:
    module = importlib.import_module("mlody.common")
    assert module.__name__ == "mlody.common"


def test_bare_common_cannot_load_mlody_common_package() -> None:
    module = importlib.import_module("mlody.common")
    assert module.__spec__ is not None
    assert module.__spec__.origin is not None

    init_path = Path(module.__spec__.origin)
    spec = importlib.util.spec_from_file_location("common", init_path)
    assert spec is not None
    assert spec.loader is not None

    wrong_name_module = importlib.util.module_from_spec(spec)
    with pytest.raises(
        ModuleNotFoundError,
        match="Use 'mlody.common', not 'common'",
    ):
        spec.loader.exec_module(wrong_name_module)
