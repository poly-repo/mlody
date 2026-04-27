"""Regression tests for canonical import paths across the repo."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import mlody.common as mlody_common


def test_mlody_common_import_path_is_available() -> None:
    assert mlody_common.__name__ == "mlody.common"


def test_bare_common_cannot_load_mlody_common_package() -> None:
    assert mlody_common.__spec__ is not None
    assert mlody_common.__spec__.origin is not None

    init_path = Path(mlody_common.__spec__.origin)
    spec = importlib.util.spec_from_file_location("common", init_path)
    assert spec is not None
    assert spec.loader is not None

    wrong_name_module = importlib.util.module_from_spec(spec)
    with pytest.raises(
        ModuleNotFoundError,
        match="Use 'mlody.common', not 'common'",
    ):
        spec.loader.exec_module(wrong_name_module)
