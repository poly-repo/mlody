"""Starlark-facing namespace factories for mlody actions."""

from __future__ import annotations

from common.python.starlarkish.core.struct import struct

from mlody.actions.build_image import build_image
from mlody.actions.execute import execute


def make_actions_struct() -> object:
    return struct(build_image=build_image, execute=execute)
