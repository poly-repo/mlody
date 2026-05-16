"""Internal MlodyValue types not part of the public resolver API.

``_RawAttrValue`` is the terminal result of attribute-path traversal when no
typed promotion applies.  CLI modules import it directly from this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mlody.resolver.values.base import MlodyValue

if TYPE_CHECKING:
    from mlody.core.label.label import Label


@dataclass(frozen=True)
class _RawAttrValue(MlodyValue):
    """Internal: terminal value reached after attribute-path traversal."""

    value: object
    label: "Label"
