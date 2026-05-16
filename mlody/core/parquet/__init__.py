"""mlody.core.parquet — Parquet deserializer for label-path traversal.

This package provides ``ParquetDeserializer``, a lazy reader for single-file
local Parquet datasets that maps typed ``PathSegment`` objects (index, slice,
field) to pyarrow row-range reads.  It also exposes a global extension registry
so callers can override the default opaque-type sentinel (``'<image>'``) for
specific pyarrow column types (e.g. HF Image structs).

Usage example::

    from mlody.core.parquet import (
        ParquetDeserializer,
        register_parquet_handler,
        OPAQUE_SENTINEL,
    )
    import pyarrow as pa
    from PIL import Image as PILImage

    def _decode_image(value: object, field: pa.Field) -> PILImage.Image:
        # Custom handler: convert a HuggingFace image struct to a PIL Image.
        ...

    register_parquet_handler(pa.struct([("bytes", pa.binary())]), _decode_image)

    ds = ParquetDeserializer("/data/train.parquet")
    row = ds[0]                  # dict[str, Any] for the first row
    rows = ds[10:20]             # list[dict] for rows 10–19
    loss = ds[0]["loss"]         # scalar from a specific column

Public re-exports: ``ParquetDeserializer``, ``register_parquet_handler``,
``OPAQUE_SENTINEL``, ``convert_arrow_value``.
"""

from mlody.core.parquet.deserializer import (
    OPAQUE_SENTINEL as OPAQUE_SENTINEL,
    ParquetDeserializer as ParquetDeserializer,
    _clear_handlers as _clear_handlers,
    convert_arrow_value as convert_arrow_value,
    read_file_as_rows as read_file_as_rows,
    register_parquet_handler as register_parquet_handler,
)
