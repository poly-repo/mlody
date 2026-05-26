"""show subcommand — resolve and display pipeline values."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import pwd
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import click
import networkx
import pyarrow as pa
from rich.cells import cell_len
from rich.console import Console
from rich.measure import Measurement
from rich.pretty import pretty_repr
from rich.segment import Segment
from rich.table import Table

from common.python.console import RichDomNode, RichDomExecutor, SyntaxNode, panel

from mlody.cli.asset_render import (
    asset_metadata_payload,
    build_asset_metadata_console_table,
)
from mlody.cli.action_graph_render import build_action_graph_table
from mlody.cli.dag_render import build_dag_table, render_dag_table, resolve_show_output_selection
from mlody.cli.show_execution import (
    PreparedShowValue,
    TabularPreviewFailure,
    execute_show_action_graph,
)
from mlody.cli.lineage_render import (
    build_lineage_console_table,
    is_lineage_type,
    lineage_rows_from_payload,
)
from mlody.cli.main import cli
from mlody.core.dag import build_dag
from mlody.core.action_graph import ActionGraphSelection
from mlody.core.derived import DerivedValueShapeError
from mlody.core.label.label import Label as _Label
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular.derived_source import DerivedSource
from mlody.core.tabular.interfaces import PreviewResult, TabularSource
from mlody.core.tabular.location_specs import source_from_value
from mlody.core.workspace import Workspace, WorkspaceLoadError, force
from mlody.db.evaluations import open_db, write_evaluation
from mlody.db.local_diff import compute_local_diff_sha, get_repo_root
from mlody.db.local_patches import write_local_patch
from mlody.resolver import (
    MlodyActionValue,
    MlodyFolderValue,
    MlodySourceValue,
    MlodyTaskValue,
    MlodyUnresolvedValue,
    MlodyValue,
    MlodyValueValue,
    MlodyVectorValue,
    MlodyWorkspaceValue,
    apply_workspace_user,
    configure_workspace,
    resolve_label_to_value,
    resolve_workspace,
)
from mlody.resolver.errors import WorkspaceResolutionError
from mlody.resolver.render import dom_for

_logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SUFFIX = Path(".cache") / "mlody"
_DEFAULT_DB_NAME = "mlody.sqlite"
_DEFAULT_WORKSPACES_SUFFIX = _DEFAULT_CACHE_SUFFIX / "workspaces"
_console = Console()


@dataclasses.dataclass(frozen=True)
class _TerminalImageEncoder:
    """Encode decoded images for terminal display.

    ``supports_rich_tables`` indicates that the encoded payload can be emitted
    as a zero-width control segment while a textual label drives Rich's layout.
    """

    encode: Callable[[object], str | None]
    encode_with_placement: Callable[[object, int, int], str | None] | None = None
    supports_rich_tables: bool = False
    rich_table_target_rows: int = 1
    rich_table_cell_aspect: float = 1.0

    def __call__(self, image: object) -> str | None:
        return self.encode(image)

    def encode_for_table(
        self,
        image: object,
        *,
        columns: int,
        rows: int,
    ) -> str | None:
        if self.encode_with_placement is not None:
            return self.encode_with_placement(image, columns, rows)
        return self.encode(image)


@dataclasses.dataclass(frozen=True)
class _PreparedCell:
    """Precomputed display data for one tabular preview cell."""

    label: str
    encoded: str | None = None
    is_image: bool = False
    display_width: int = 0
    display_height: int = 1


@dataclasses.dataclass(frozen=True)
class _RichRenderableNode:
    """Adapts an arbitrary Rich renderable into a RichDomNode for use in DOM tables."""

    renderable: object

    def render(self, ctx: object) -> object:
        return self.renderable


@dataclasses.dataclass(frozen=True)
class _RichTableImageCell:
    """Rich renderable for table-safe terminal image cells."""

    encoded: str
    width: int
    height: int

    def __rich_measure__(self, console, options) -> Measurement:
        width = max(1, self.width)
        return Measurement(width, width)

    def __rich_console__(self, console, options):
        # Treat the terminal-image payload as zero-width so Rich sizes the cell
        # from the configured cell footprint instead of the raw escape
        # sequence. Render a blank spacer block so the cell stays visually
        # clean in image-capable terminals while Rich reserves the intended
        # 4×4 character area for the image.
        width = max(1, self.width)
        height = max(1, self.height)
        yield Segment(self.encoded, None, (("__mlody_terminal_image__",),))
        yield "\n".join([" " * width] * height)


def _get_username() -> str:
    """Return the OS username; falls back to pwd lookup if os.getlogin() raises."""
    try:
        return os.getlogin()
    except OSError:
        return pwd.getpwuid(os.getuid()).pw_name


def _display_payload(value: MlodyValueValue) -> object:
    """Return the printable payload for a resolved mlody value.

    Virtual values stay lazy through resolution, but the CLI should force them
    when the user explicitly asks to display them.
    """
    return force(value.struct)


def _concrete_show_label(committoid: str | None, label_text: str) -> object:
    """Build the parsed label object for one expanded show target."""
    from mlody.core.label import parse_label as _core_parse_label

    if label_text == "":
        return _Label(
            workspace=committoid,
            workspace_query=None,
            entity=None,
            entity_query=None,
            attribute_path=None,
            attribute_query=None,
        )
    return _core_parse_label(label_text)


def _prepared_display_payload(
    value: MlodyValueValue,
    prepared: PreparedShowValue | None = None,
) -> object:
    if prepared is not None:
        return prepared.display_payload
    return _display_payload(value)


def _read_meta(cache_root: Path, resolved_sha: str) -> dict[str, object]:
    """Read the -meta.json file written by materialise(), returning {} on failure."""
    meta_path = cache_root / f"{resolved_sha}-meta.json"
    try:
        return dict(json.loads(meta_path.read_text()))  # type: ignore[arg-type]
    except Exception:
        return {}


def _record_evaluation(
    resolved_sha: str,
    requested_ref: str,
    local_only: bool,
    repo: str,
    resolved_at: str,
    value_description: str,
    patch: str = "",
) -> None:
    """Write one evaluation row (and optionally a local_patches row) to the local SQLite DB.

    Best-effort: logs at ERROR level and returns on any failure so a DB error
    never terminates the show command (NFR-AVAIL-001). Connection is always
    closed in the finally block.
    """
    db_path = Path.home() / _DEFAULT_CACHE_SUFFIX / _DEFAULT_DB_NAME
    conn = None
    try:
        conn = open_db(db_path)
        local_diff_sha = compute_local_diff_sha(get_repo_root())
        local_patch_sha = write_local_patch(conn, patch)
        write_evaluation(
            conn,
            username=_get_username(),
            hostname=socket.gethostname(),
            requested_ref=requested_ref,
            resolved_sha=resolved_sha,
            resolved_at=resolved_at,
            repo=repo,
            local_only=local_only,
            value_description=value_description,
            local_diff_sha=local_diff_sha,
            local_patch_sha=local_patch_sha,
        )
    except Exception as exc:
        _logger.error("Failed to write evaluation to %s: %s", db_path, exc)
    finally:
        if conn is not None:
            conn.close()


def _head_sha_from_workspace(workspace: Workspace) -> str | None:
    ws_ctx = getattr(getattr(workspace.evaluator, "_extra_ctx", None), "workspace", None)
    sha = str(getattr(ws_ctx, "commit", "") or "")
    return sha if len(sha) == 40 else None


def show_fn(
    label: str,
    monorepo_root: Path,
    workspace_root: Path | None = None,
    roots_file: Path | None = None,
    full_workspace: bool = False,
    print_fn: Callable[..., None] = print,
    verbose: bool = False,
) -> object:
    """Resolve a label to a value via a fresh workspace.

    Used by the shell REPL. Accepts a raw label (with optional committoid prefix)
    and constructs a workspace independently for each call.
    """
    workspace, _sha = resolve_workspace(
        label,
        monorepo_root=monorepo_root,
        workspace_root=workspace_root,
        roots_file=roots_file,
        full_workspace=full_workspace,
        print_fn=print_fn,
        verbose=verbose,
    )
    _committoid, inner_label = _parse_inner(label)
    print_fn(pretty_repr(_parse_label_struct(label)))
    concrete_label = _concrete_show_label(_committoid, inner_label)
    execution = execute_show_action_graph(
        workspace,
        inner_label,
        concrete_label,
        resolve_label=resolve_label_to_value,
    )
    return execution.prepared_value.value


def _parse_inner(label: str) -> tuple[str | None, str]:
    """Extract committoid and inner label without raising — delegates to parse_label."""
    from mlody.resolver.resolver import parse_label

    return parse_label(label)


def _parse_label_struct(label: str) -> object:
    """Return the parsed Label struct for display purposes."""
    from mlody.core.label import parse_label as _core_parse_label

    return _core_parse_label(label)


def _is_primitive(value: object) -> bool:
    return isinstance(value, str | int | float | bool)


def _to_pil_image(value: object):  # -> PIL.Image.Image | None
    """Decode a HuggingFace-style image cell to a PIL Image.

    Accepts ``{'bytes': <bytes>, ...}`` dicts (the format HuggingFace datasets
    use for image columns) or raw ``bytes`` that begin with a known image magic.
    Returns ``None`` if the value is not recognisable as image data.
    """
    try:
        from PIL import Image as _PIL  # noqa: PLC0415
        import io as _io  # noqa: PLC0415

        raw: bytes | None = None
        if isinstance(value, dict):
            raw = value.get("bytes")
        elif isinstance(value, bytes):
            raw = value
        if not raw:
            return None
        # Quick magic check: PNG, JPEG, GIF, WEBP, BMP
        if not (
            raw[:4] in (b"\x89PNG", b"GIF8", b"RIFF", b"BM\x00\x00")
            or raw[:2] == b"\xff\xd8"
        ):
            return None
        return _PIL.open(_io.BytesIO(raw))
    except Exception:
        return None


def _can_kitty() -> bool:
    """Return True when running inside the Kitty terminal emulator."""
    return (
        os.environ.get("TERM") == "xterm-kitty"
        or bool(os.environ.get("KITTY_WINDOW_ID"))
    ) and sys.stdout.isatty()


def _can_sixel() -> bool:
    """Return True when the terminal is known to support Sixel graphics."""
    try:
        vte = int(os.environ.get("VTE_VERSION", "0"))
        return vte >= 6500 and sys.stdout.isatty()
    except Exception:
        return False


def _kitty_encode(
    img,
    *,
    max_width: int = 640,
    cell_columns: int | None = None,
    cell_rows: int | None = None,
    cell_aspect: float = 2.0,
    no_cursor_movement: bool = False,
) -> str | None:  # img: PIL.Image.Image
    """Encode a PIL Image using the Kitty terminal graphics protocol.

    Transmits the image as a sequence of base64-encoded PNG chunks (≤ 4096
    bytes each) wrapped in APC escape sequences (``ESC _G … ESC \\``).

    Returns ``None`` on any failure so callers can fall back to Sixel.
    """
    try:
        from PIL import Image as _PIL  # noqa: PLC0415
        import base64 as _b64  # noqa: PLC0415
        import io as _io  # noqa: PLC0415

        w, h = img.size
        if w > max_width:
            h = max(1, int(h * max_width / w))
            img = img.resize((max_width, h), _PIL.LANCZOS)
            w, h = img.size

        if cell_columns is not None and cell_rows is not None:
            target_aspect = cell_columns / max(1.0, cell_rows * cell_aspect)
            image_aspect = w / max(1.0, h)
            if abs(image_aspect - target_aspect) > 0.01:
                if image_aspect > target_aspect:
                    padded_height = max(h, math.ceil(w / target_aspect))
                    canvas = _PIL.new("RGBA", (w, padded_height), (0, 0, 0, 0))
                    canvas.paste(img.convert("RGBA"), (0, (padded_height - h) // 2))
                else:
                    padded_width = max(w, math.ceil(h * target_aspect))
                    canvas = _PIL.new("RGBA", (padded_width, h), (0, 0, 0, 0))
                    canvas.paste(img.convert("RGBA"), ((padded_width - w) // 2, 0))
                img = canvas
                w, h = img.size

        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        png_bytes = buf.getvalue()

        b64 = _b64.standard_b64encode(png_bytes).decode("ascii")

        CHUNK = 4096
        chunks = [b64[i : i + CHUNK] for i in range(0, len(b64), CHUNK)]
        if not chunks:
            return None

        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            more = 0 if i == len(chunks) - 1 else 1
            if i == 0:
                # First chunk: action=T (transmit+display), f=100 (PNG), q=1 (quiet)
                placement_parts = ["a=T", "f=100", "q=1"]
                if cell_columns is not None:
                    placement_parts.append(f"c={max(1, cell_columns)}")
                if cell_rows is not None:
                    placement_parts.append(f"r={max(1, cell_rows)}")
                if no_cursor_movement:
                    placement_parts.append("C=1")
                placement_parts.append(f"m={more}")
                parts.append("\x1b_G" + ",".join(placement_parts) + f";{chunk}\x1b\\")
            else:
                parts.append(f"\x1b_Gm={more};{chunk}\x1b\\")

        return "".join(parts)
    except Exception:
        return None


def _sixel_encode(img, *, max_width: int = 320) -> str | None:  # img: PIL.Image.Image
    """Encode a PIL Image as a Sixel escape sequence (DCS...ST).

    Resizes to *max_width* columns, quantises to 256 palette colours, and
    emits RLE-compressed Sixel data.  Returns ``None`` on any failure so
    callers can fall back gracefully.
    """
    try:
        from PIL import Image as _PIL  # noqa: PLC0415

        w, h = img.size
        if w > max_width:
            h = max(1, int(h * max_width / w))
            img = img.resize((max_width, h), _PIL.LANCZOS)
            w, h = img.size

        # Pad height to the next multiple of 6 (Sixel band height).
        pad_h = (h + 5) // 6 * 6
        if pad_h != h:
            canvas = _PIL.new("RGB", (w, pad_h), 0)
            canvas.paste(img)
            img = canvas
            h = pad_h

        q = img.convert("RGB").quantize(colors=256, method=_PIL.Quantize.MEDIANCUT)
        pal = q.getpalette()  # flat [R, G, B, …]; length = num_used_colors × 3
        num_colors = len(pal) // 3
        pixels = q.load()  # PixelAccess: pixels[x, y] → palette index

        parts: list[str] = ["\x1bPq"]

        # Emit colour register definitions for every palette entry present.
        for ci in range(num_colors):
            r, g, b = pal[ci * 3], pal[ci * 3 + 1], pal[ci * 3 + 2]
            parts.append(f"#{ci};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")

        # Encode image as consecutive 6-row bands.
        for band in range(h // 6):
            y0 = band * 6
            # Build a per-colour array of sixel characters for this band.
            color_rows: dict[int, bytearray] = {}
            for x in range(w):
                bits_by_color: dict[int, int] = {}
                for dy in range(6):
                    ci = pixels[x, y0 + dy]
                    bits_by_color[ci] = bits_by_color.get(ci, 0) | (1 << dy)
                for ci, bits in bits_by_color.items():
                    if ci not in color_rows:
                        color_rows[ci] = bytearray(b"?" * w)  # 0x3F = no bits set
                    color_rows[ci][x] = 0x3F + bits

            first_color = True
            for ci in sorted(color_rows):
                if not first_color:
                    parts.append("$")  # carriage-return: restart at column 0
                first_color = False
                parts.append(f"#{ci}")
                row = color_rows[ci]
                i = 0
                while i < w:
                    c = row[i]
                    j = i + 1
                    while j < w and row[j] == c:
                        j += 1
                    run = j - i
                    ch = chr(c)
                    parts.append(f"!{run}{ch}" if run > 3 else ch * run)
                    i = j
            parts.append("-")  # advance to next sixel band

        parts.append("\x1b\\")  # DCS string terminator
        return "".join(parts)
    except Exception:
        return None


def _image_encoder_supports_rich_tables(image_encoder) -> bool:
    """Return True when *image_encoder* can be used safely inside Rich tables."""
    return bool(getattr(image_encoder, "supports_rich_tables", False))


def _contains_terminal_control(text: str | None) -> bool:
    """Return True when *text* contains terminal control sequences."""
    return bool(text and "\x1b" in text)


def _prepare_cell(value: object, *, image_encoder=None) -> _PreparedCell:
    """Return display metadata for one tabular preview cell.

    If *image_encoder* is provided and the value is image data, the encoded
    terminal payload is preserved separately from the textual fallback label.
    """
    if _is_image_cell(value):
        img = _to_pil_image(value)
        if img is not None:
            encoded = None
            display_width = max(
                1, cell_len(f"<{img.format or 'image'} {img.width}×{img.height}>")
            )
            display_height = 1
            if image_encoder is not None:
                if _image_encoder_supports_rich_tables(image_encoder):
                    display_height = max(
                        1,
                        int(getattr(image_encoder, "rich_table_target_rows", 1)),
                    )
                    display_width = max(
                        1,
                        math.ceil(
                            display_height
                            * img.width
                            / max(1, img.height)
                            * float(
                                getattr(image_encoder, "rich_table_cell_aspect", 1.0)
                            )
                        ),
                    )
                    try:
                        encoded = image_encoder.encode_for_table(
                            img,
                            columns=display_width,
                            rows=display_height,
                        )
                    except Exception:
                        encoded = None
                else:
                    try:
                        encoded = image_encoder(img)
                    except Exception:
                        encoded = None
            return _PreparedCell(
                label=f"<{img.format or 'image'} {img.width}×{img.height}>",
                encoded=encoded or None,
                is_image=True,
                display_width=display_width,
                display_height=display_height,
            )
        if isinstance(value, dict):
            return _PreparedCell(
                label=f"<image {len(value['bytes'])} bytes>",
                is_image=True,
                display_width=max(1, cell_len(f"<image {len(value['bytes'])} bytes>")),
            )
        return _PreparedCell(
            label=f"<bytes {len(value)}>",
            is_image=True,
            display_width=max(1, cell_len(f"<bytes {len(value)}>")),
        )
    label = str(value)
    return _PreparedCell(label=label, display_width=max(1, cell_len(label)))


def _prepared_cell_display(cell: _PreparedCell) -> str:
    """Return the best printable text for a prepared cell."""
    return cell.encoded or cell.label


def _cell_label(value: object, *, image_encoder=None) -> str:
    """Return a displayable string for one table cell."""
    return _prepared_cell_display(_prepare_cell(value, image_encoder=image_encoder))


def _build_tabular_preview(
    table: pa.Table,
    image_encoder: object = None,
) -> tuple[list[str], list[list[object]], int]:
    """Convert a pyarrow Table into the (col_names, data_rows, total_rows) tuple
    stored as _tabular_preview in dispatch structs.

    For image-capable terminals each image cell is stored as a _RichRenderableNode
    wrapping a _RichTableImageCell so the DOM table can render it inline.
    For all other cells the cell is stored as a plain string.
    """
    pydict = table.to_pydict()
    col_names = list(table.column_names)
    data_rows: list[list[object]] = []
    for i in range(table.num_rows):
        row: list[object] = []
        for col in col_names:
            cell = _prepare_cell(pydict[col][i], image_encoder=image_encoder)
            if (
                cell.is_image
                and cell.encoded is not None
                and _image_encoder_supports_rich_tables(image_encoder)
            ):
                row.append(
                    _RichRenderableNode(
                        _RichTableImageCell(
                            encoded=cell.encoded,
                            width=cell.display_width,
                            height=cell.display_height,
                        )
                    )
                )
            else:
                row.append(_prepared_cell_display(cell))
        data_rows.append(row)
    return col_names, data_rows, table.num_rows


def _is_render_spec(obj: object) -> bool:
    return getattr(obj, "kind", None) == "render_value_spec"


def _get_element_type_name(type_struct: object) -> str | None:
    """Return the element type name for a vector type struct, or None."""
    attrs = getattr(type_struct, "attributes", None)
    element_type = attrs.get("element_type") if isinstance(attrs, dict) else None
    if element_type is None:
        element_type = getattr(type_struct, "element_type", None)
    if element_type is None:
        return None
    elem_name: str | None = getattr(element_type, "name", None) or getattr(element_type, "type_name", None)
    if elem_name is None and isinstance(element_type, str) and element_type.startswith(":"):
        elem_name = element_type[1:]
    return elem_name


def _render_elements_individually(
    type_struct: object,
    raw_preview: object,
    workspace: object,
    dom_executor: object,
) -> bool:
    """Dispatch render_element once per row and render each result.

    Returns True if a render_element method was found and used (even if some
    rows returned None and were skipped). Returns False when no method is
    registered for this element type, so the caller can fall back.
    """
    from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    elem_name = _get_element_type_name(type_struct)
    if elem_name is None:
        return False

    entry = workspace.evaluator._method_registry.get("render_element", {})
    methods = list(entry.get("methods", []))
    if not methods:
        return False

    col_names, data_rows, _total = raw_preview
    matched = False
    for row in data_rows:
        elem_arg = Struct(kind="type", name=elem_name, _row=list(zip(col_names, row)))
        try:
            result = dispatch("render_element", (elem_arg,), methods)
            matched = True
            if result is not None and _is_render_spec(result):
                dom_executor.render(_render_spec_to_dom(result))
        except DispatchError:
            return False
    return matched


def _apply_element_preview(
    type_struct: object,
    raw_preview: object,
    workspace: object,
) -> object:
    """Dispatch render_element_preview for the vector element type.

    Builds a dispatch arg struct(kind="type", name=<elem-name>, _preview=<tuple>)
    and fires render_element_preview. Returns whatever the method body returns —
    a render_value_spec struct, a transformed (col_names, rows, total) tuple, or
    None — or None when no method is registered / no element type is found.
    """
    from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415
    from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

    elem_name = _get_element_type_name(type_struct)
    if elem_name is None:
        return None

    entry = workspace.evaluator._method_registry.get("render_element_preview", {})
    methods = list(entry.get("methods", []))
    if not methods:
        return None

    elem_dispatch_arg = Struct(kind="type", name=elem_name, _preview=raw_preview)
    try:
        return dispatch("render_element_preview", (elem_dispatch_arg,), methods)
    except DispatchError:
        return None


def _image_encoder_for_terminal():
    """Return an image encoder callable for the current terminal, or None."""
    if _can_kitty():
        return _TerminalImageEncoder(
            encode=lambda img: _kitty_encode(
                img,
                max_width=160,
                cell_rows=4,
                no_cursor_movement=True,
            ),
            encode_with_placement=lambda img, columns, rows: _kitty_encode(
                img,
                max_width=160,
                cell_columns=columns,
                cell_rows=rows,
                cell_aspect=2.0,
                no_cursor_movement=True,
            ),
            supports_rich_tables=True,
            rich_table_target_rows=4,
            # Terminal cells are typically about twice as tall as they are wide,
            # so reserving ~2 columns per row yields a squarer on-screen image.
            rich_table_cell_aspect=2.0,
        )
    if _can_sixel():
        return _TerminalImageEncoder(
            encode=lambda img: _sixel_encode(img, max_width=80),
            supports_rich_tables=False,
        )
    return None


def _is_image_cell(value: object) -> bool:
    """Return True when *value* looks like an image payload cell."""
    return (
        isinstance(value, dict) and isinstance(value.get("bytes"), bytes)
    ) or isinstance(value, bytes)


def _format_row_preview(
    prepared_rows: list[dict[str, _PreparedCell]],
    column_names: list[str],
    header: str,
    display_total: int,
    rows: int,
) -> str:
    """Render tabular previews row-by-row.

    This is used when embedding cells in a Rich table would be unreadable or
    would corrupt terminal layout.
    """
    lines = [header]
    for i, row in enumerate(prepared_rows):
        lines.append(f"[{i}]")
        for column_name in column_names:
            cell = row[column_name]
            cell_text = _prepared_cell_display(cell)
            if cell.is_image and cell.encoded is not None:
                lines.append(f"  {column_name}:")
                lines.append(cell_text)
            else:
                lines.append(f"  {column_name}: {cell_text}")

    if display_total > rows:
        lines.append(f"… ({display_total - rows} more rows not shown)")
    return "\n".join(lines)


def _should_use_row_preview(
    prepared_rows: list[dict[str, _PreparedCell]],
    column_names: list[str],
) -> bool:
    """Return whether a tabular preview is too wide for a readable Rich table."""
    if len(column_names) > 16:
        return True

    console_width = max(_console.width, 40)
    # Account for borders / separators while keeping the estimate conservative.
    estimated_width = 1
    for column_name in column_names:
        sample_width = cell_len(column_name)
        for row in prepared_rows[:5]:
            sample_width = max(sample_width, row[column_name].display_width)
        estimated_width += min(max(sample_width, 4), 20) + 3

    return estimated_width > console_width


def _format_value(
    value: object, *, total_rows: int | None = None, image_encoder=None
) -> str:
    if isinstance(value, pa.Table):
        rows = value.num_rows
        display_total = total_rows if total_rows is not None else rows
        cols = value.num_columns
        preview = value.slice(0, 50)
        header = f"pyarrow.Table  {display_total} rows × {cols} columns"
        if not preview.column_names:
            return header

        data_rows = preview.to_pydict()
        prepared_rows = [
            {
                column_name: _prepare_cell(
                    data_rows[column_name][i],
                    image_encoder=image_encoder,
                )
                for column_name in preview.column_names
            }
            for i in range(preview.num_rows)
        ]
        if any(
            _contains_terminal_control(cell.encoded)
            and not _image_encoder_supports_rich_tables(image_encoder)
            for row in prepared_rows
            for cell in row.values()
        ):
            return _format_row_preview(
                prepared_rows,
                list(preview.column_names),
                header=header,
                display_total=display_total,
                rows=rows,
            )

        if _should_use_row_preview(prepared_rows, list(preview.column_names)):
            return _format_row_preview(
                prepared_rows,
                list(preview.column_names),
                header=header,
                display_total=display_total,
                rows=rows,
            )

        table = Table(title=header)
        for column_name in preview.column_names:
            table.add_column(column_name, overflow="fold")

        for row in prepared_rows:
            table.add_row(
                *[
                    _RichTableImageCell(
                        encoded=cell.encoded or "",
                        width=cell.display_width,
                        height=cell.display_height,
                    )
                    if _contains_terminal_control(cell.encoded)
                    and _image_encoder_supports_rich_tables(image_encoder)
                    else _prepared_cell_display(cell)
                    for cell in (
                        row[column_name] for column_name in preview.column_names
                    )
                ]
            )

        if display_total > rows:
            table.caption = f"… ({display_total - rows} more rows not shown)"

        with _console.capture() as capture:
            _console.print(table)
        return capture.get().rstrip()
    if _is_primitive(value):
        return str(value)
    return pretty_repr(value)


def _pretty_struct_str(obj: object, _depth: int = 0) -> str:
    """Recursively format a Starlark struct into an indented Python-like string.

    Private fields (starting with ``_``) and callable values (validators etc.)
    are omitted to keep the output readable.
    """
    pad = "    " * _depth
    inner = "    " * (_depth + 1)

    if hasattr(obj, "as_mapping"):
        fields = {k: v for k, v in obj.as_mapping().items() if not k.startswith("_")}
        if not fields:
            return "struct()"
        parts = [
            f"{inner}{k}={_pretty_struct_str(v, _depth + 1)}" for k, v in fields.items()
        ]
        return "struct(\n" + ",\n".join(parts) + f",\n{pad})"

    if isinstance(obj, list):
        if not obj:
            return "[]"
        parts = [f"{inner}{_pretty_struct_str(v, _depth + 1)}" for v in obj]
        return "[\n" + ",\n".join(parts) + f",\n{pad}]"

    if isinstance(obj, dict):
        if not obj:
            return "{}"
        parts = [
            f"{inner}{k!r}: {_pretty_struct_str(v, _depth + 1)}" for k, v in obj.items()
        ]
        return "{\n" + ",\n".join(parts) + f",\n{pad}}}"

    if callable(obj) and not isinstance(obj, type):
        return "<callable>"

    return repr(obj)


def _raw_json_blob(payload: object, *, name: object) -> str | None:
    """Return a pretty JSON blob for declared ``raw`` values."""
    if name != "raw" or not isinstance(payload, str):
        return None
    try:
        parsed = json.loads(payload)
    except Exception:
        return None
    return json.dumps(parsed, indent=2, sort_keys=True)


def _print_row_list(rows: list, *, image_encoder=None) -> None:
    """Display row-list results using the same table preview as tabular sources."""
    if rows and all(isinstance(row, dict) for row in rows):
        try:
            table = pa.Table.from_pylist(rows)
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
            table = None
        if table is not None:
            click.echo(_format_value(table, image_encoder=image_encoder))
            return

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            click.echo(repr(row))
            continue
        click.echo(f"[{i}]")
        for k, v in row.items():
            click.echo(f"  {k}: {_cell_label(v, image_encoder=image_encoder)}")


def _emit_tabular_preview(preview: PreviewResult) -> None:
    """Render a tabular preview to the terminal."""
    click.echo(
        _format_value(
            preview.table,
            total_rows=preview.total_rows,
            image_encoder=_image_encoder_for_terminal(),
        )
    )


def _print_tabular_source(
    source: object,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
    """Render a tabular source when supported; return True on handled output."""
    if isinstance(source, DerivedSource):
        try:
            _emit_tabular_preview(source.preview(50))
        except DerivedValueShapeError as exc:
            click.echo(
                click.style(
                    f"Error: derived query produced a scalar result — {exc}",
                    fg="red",
                ),
                err=True,
            )
            if _has_error is not None:
                _has_error.append(True)
        except MlodyQueryError as exc:
            click.echo(
                click.style(f"Error: {exc}", fg="red"),
                err=True,
            )
            if _has_error is not None:
                _has_error.append(True)
        return True

    if isinstance(source, TabularSource):
        _ = _has_error
        try:
            _emit_tabular_preview(source.preview(50))
            return True
        except Exception:
            return False

    _ = _has_error
    return False


def _render_spec_to_dom(spec: object) -> RichDomNode:
    """Convert a render_value_spec struct returned by a Starlark method body to a Rich DOM node."""
    from common.python.console import panel, stack, table, text  # noqa: PLC0415

    sections = list(getattr(spec, "sections", None) or [])
    title = str(getattr(spec, "title", "value") or "value")
    nodes: list[RichDomNode] = []
    for s in sections:
        rows = list(getattr(s, "rows", None) or [])
        code = getattr(s, "code", None)
        if rows:
            nodes.append(
                table(
                    [str(getattr(s, "name", "")), ""],
                    [[text(str(r[0])), text(str(r[1]))] for r in rows],
                )
            )
        if code is not None:
            language = str(getattr(s, "language", "python") or "python")
            nodes.append(SyntaxNode(str(code), language=language))
        tabular_preview = getattr(s, "tabular_preview", None)
        if tabular_preview is not None:
            col_names, data_rows, _total_rows = tabular_preview
            nodes.append(
                table(
                    col_names,
                    [
                        [
                            cell if hasattr(cell, "render") else text(str(cell))
                            for cell in row
                        ]
                        for row in data_rows
                    ],
                )
            )
    return panel(stack(*nodes) if nodes else text("(no content)"), title=title)


def _is_dag_value(value: MlodyValue) -> bool:
    if not isinstance(value, MlodyValueValue):
        return False

    from mlody.core.dag_value import MlodyDagType  # noqa: PLC0415

    return isinstance(getattr(value.struct, "type", None), MlodyDagType)


def _is_action_graph_value(value: MlodyValue) -> bool:
    if not isinstance(value, MlodyValueValue):
        return False

    from mlody.core.action_graph_value import MlodyActionGraphType  # noqa: PLC0415

    return isinstance(getattr(value.struct, "type", None), MlodyActionGraphType)


def _dag_title_for_value(value: MlodyValueValue) -> str:
    dag_label = getattr(value.struct, "label", "") or ""
    display_label = dag_label.removesuffix(".dag")
    if display_label and display_label != "dag":
        return f"DAG — ancestors of '{display_label}'"

    port_name = getattr(value.struct, "name", "") or ""
    return f"DAG — ancestors of '{port_name}'" if port_name else "DAG"


def _action_graph_title_for_value(value: MlodyValueValue) -> str:
    action_label = getattr(value.struct, "label", "") or ""
    display_label = action_label.removesuffix(".agraph")
    if display_label and display_label != "agraph":
        return f"Action Graph — plan for '{display_label}'"

    port_name = getattr(value.struct, "name", "") or ""
    return (
        f"Action Graph — plan for '{port_name}'"
        if port_name
        else "Action Graph"
    )


def _print_mlody_value(
    value: MlodyValue,
    *,
    workspace: Workspace | None = None,
    _has_error: list[bool] | None = None,
    prepared: PreparedShowValue | None = None,
) -> None:
    """Print a MlodyValue to the console.

    Data-backed values (parquet, derived) are rendered inline via click.echo.
    MlodyValueValue entities are rendered via the render_value multimethod when a
    workspace is provided; all other structural values are rendered via dom_for
    from mlody.resolver.render.
    """
    dom_executor = RichDomExecutor(_console)

    if isinstance(value, MlodyVectorValue):
        prepared_children = prepared.children if prepared is not None else ()
        for index, elem in enumerate(value.elements):
            child_prepared = (
                prepared_children[index] if index < len(prepared_children) else None
            )
            _print_mlody_value(
                elem,
                workspace=workspace,
                _has_error=_has_error,
                prepared=child_prepared,
            )
        return

    from mlody.resolver.values.structural import MlodySourceRangeValue  # noqa: PLC0415

    if isinstance(value, MlodySourceRangeValue):
        if workspace is not None:
            from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415
            from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

            entry = workspace.evaluator._method_registry.get("render_value", {})
            methods = list(entry.get("methods", []))
            if methods:
                try:
                    lines_text = value.abs_path.read_text().splitlines()
                    snippet = "\n".join(
                        lines_text[value.start_line - 1 : value.end_line]
                    )
                except Exception:
                    snippet = f"(could not read {value.abs_path})"
                sr_struct = Struct(
                    kind="mlody-source-range",
                    filepath=value.filepath,
                    start_line=value.start_line,
                    end_line=value.end_line,
                    content=snippet,
                )
                try:
                    spec = dispatch("render_value", (sr_struct,), methods)
                    _logger.debug(
                        "render_value: dispatch succeeded for source-range %r",
                        value.filepath,
                    )
                    dom_executor.render(_render_spec_to_dom(spec))
                    return
                except DispatchError:
                    _logger.debug(
                        "render_value: no method for source-range, falling back"
                    )
        dom_executor.render(dom_for(value))
        return

    if isinstance(value, MlodyValueValue):
        from mlody.core.virtual_value import force_virtual_value  # noqa: PLC0415

        if _is_dag_value(value):
            graph = (
                prepared.display_payload
                if prepared is not None
                else force_virtual_value(value.struct)
            )
            if isinstance(graph, networkx.MultiDiGraph):
                _console.print(build_dag_table(graph, _dag_title_for_value(value)))
            return
        if _is_action_graph_value(value):
            graph = (
                prepared.display_payload
                if prepared is not None
                else force_virtual_value(value.struct)
            )
            if isinstance(graph, networkx.DiGraph):
                _console.print(
                    build_action_graph_table(
                        graph,
                        _action_graph_title_for_value(value),
                    )
                )
            return

        display_payload = _prepared_display_payload(value, prepared)
        if is_lineage_type(getattr(value.struct, "type", None)):
            lineage_rows = lineage_rows_from_payload(display_payload)
            if lineage_rows is not None:
                dom_executor.render(
                    panel(
                        _RichRenderableNode(build_lineage_console_table(lineage_rows)),
                        title="lineage",
                    )
                )
                return

        if prepared is not None and prepared.source_failure is not None:
            click.echo(
                click.style(f"Error: {prepared.source_failure}", fg="red"),
                err=True,
            )
            if _has_error is not None:
                _has_error.append(True)
            return

        render_dispatch_value = value.struct
        if display_payload is not value.struct:
            if hasattr(display_payload, "as_mapping") and getattr(display_payload, "kind", None) == "value":
                render_dispatch_value = display_payload
            else:
                render_dispatch_value = None

        tabular_source = None
        preview_table: pa.Table | None = None
        preview_total_rows: int | None = None
        preview_failure: TabularPreviewFailure | None = None
        if prepared is not None:
            preview_table = prepared.preview_table
            preview_total_rows = prepared.preview_total_rows
            preview_failure = prepared.preview_failure
        else:
            try:
                tabular_source = (
                    source_from_value(display_payload)
                    if hasattr(display_payload, "as_mapping")
                    else None
                )
            except ValueError as exc:
                click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
                if _has_error is not None:
                    _has_error.append(True)
                return

        extra_fields: dict[str, object] = {}

        if preview_table is not None:
            extra_fields["_tabular_preview"] = _build_tabular_preview(
                preview_table,
                image_encoder=_image_encoder_for_terminal(),
            )
        elif tabular_source is not None:
            try:
                preview = tabular_source.preview(50)
                extra_fields["_tabular_preview"] = _build_tabular_preview(
                    preview.table,
                    image_encoder=_image_encoder_for_terminal(),
                )
            except Exception:
                pass

        raw_json = _raw_json_blob(
            display_payload, name=getattr(value.struct, "name", None)
        )
        if raw_json is not None:
            extra_fields["_display_json"] = raw_json

        if workspace is not None:
            from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415
            from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

            if "_tabular_preview" in extra_fields:
                type_struct = getattr(value.struct, "type", None)
                if type_struct is not None:
                    if _render_elements_individually(
                        type_struct, extra_fields["_tabular_preview"], workspace, dom_executor
                    ):
                        return
                    elem_result = _apply_element_preview(
                        type_struct, extra_fields["_tabular_preview"], workspace
                    )
                    if elem_result is not None and _is_render_spec(elem_result):
                        dom_executor.render(_render_spec_to_dom(elem_result))
                        return
                    elif elem_result is not None:
                        extra_fields["_tabular_preview"] = elem_result

            entry = workspace.evaluator._method_registry.get("render_value", {})
            methods = list(entry.get("methods", []))
            if methods and render_dispatch_value is not None:
                dispatch_struct = (
                    Struct(**{**render_dispatch_value.as_mapping(), **extra_fields})
                    if extra_fields
                    else render_dispatch_value
                )
                try:
                    spec = dispatch("render_value", (dispatch_struct,), methods)
                    _logger.debug(
                        "render_value: multimethod dispatch succeeded for %r",
                        dispatch_struct,
                    )
                    dom_executor.render(_render_spec_to_dom(spec))
                    return
                except DispatchError:
                    _logger.debug(
                        "render_value: no matching method for %r, falling back",
                        dispatch_struct,
                    )

        if preview_table is not None:
            click.echo(
                _format_value(
                    preview_table,
                    total_rows=preview_total_rows,
                    image_encoder=_image_encoder_for_terminal(),
                )
            )
            return

        if preview_failure is not None:
            if preview_failure.fatal:
                click.echo(
                    click.style(f"Error: {preview_failure.message}", fg="red"),
                    err=True,
                )
                if _has_error is not None:
                    _has_error.append(True)
                return

        if tabular_source is not None and _print_tabular_source(
            tabular_source,
            _has_error=_has_error,
        ):
            return

    from mlody.resolver.values.internal import _RawAttrValue  # noqa: PLC0415

    if isinstance(value, _RawAttrValue):
        enc = _image_encoder_for_terminal()
        raw_table: pa.Table | None = None
        if isinstance(value.value, pa.Table):
            raw_table = value.value
        elif (
            isinstance(value.value, list)
            and value.value
            and all(isinstance(r, dict) for r in value.value)
        ):
            try:
                raw_table = pa.Table.from_pylist(value.value)
            except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
                raw_table = None
        if raw_table is not None and workspace is not None:
            from mlody.core.multimethod import DispatchError, dispatch  # noqa: PLC0415
            from common.python.starlarkish.core.struct import Struct  # noqa: PLC0415

            entry = workspace.evaluator._method_registry.get("render_value", {})
            methods = list(entry.get("methods", []))
            if methods:
                label_str = (
                    value.label.format_inner()
                    if hasattr(value.label, "format_inner")
                    else str(value.label)
                )
                # When the label has a query transform (e.g. @sql), look up the
                # base entity to recover its type struct for dispatch matching.
                type_struct = None
                entity_query = getattr(value.label, "entity_query", None)
                entity = getattr(value.label, "entity", None)
                if entity_query is not None and entity is not None:
                    from mlody.core.label.label import Label as _Label  # noqa: PLC0415

                    base_label = _Label(
                        workspace=value.label.workspace,
                        workspace_query=value.label.workspace_query,
                        entity=entity,
                        entity_query=None,
                        attribute_path=value.label.attribute_path,
                        attribute_query=value.label.attribute_query,
                    )
                    try:
                        base_value = workspace.resolve(base_label.format_inner())
                        type_struct = getattr(base_value, "type", None)
                    except Exception:
                        pass
                raw_preview = _build_tabular_preview(raw_table, image_encoder=enc)
                if type_struct is not None:
                    if _render_elements_individually(type_struct, raw_preview, workspace, dom_executor):
                        return
                    elem_result = _apply_element_preview(type_struct, raw_preview, workspace)
                    if elem_result is not None and _is_render_spec(elem_result):
                        dom_executor.render(_render_spec_to_dom(elem_result))
                        return
                    elif elem_result is not None:
                        raw_preview = elem_result
                dispatch_kwargs: dict[str, object] = {
                    "kind": "value",
                    "name": label_str,
                    "_tabular_preview": raw_preview,
                }
                if type_struct is not None:
                    dispatch_kwargs["type"] = type_struct
                dispatch_struct = Struct(**dispatch_kwargs)
                try:
                    spec = dispatch("render_value", (dispatch_struct,), methods)
                    dom_executor.render(_render_spec_to_dom(spec))
                    return
                except DispatchError:
                    _logger.debug("render_value: no method for raw table, falling back")
        if raw_table is not None:
            click.echo(_format_value(raw_table, image_encoder=enc))
            return
        if isinstance(value.value, list):
            _print_row_list(value.value, image_encoder=enc)
            return
        if isinstance(value.value, dict):
            _print_row_list([value.value], image_encoder=enc)
            return

    dom_executor.render(_render_mlody_value(value, prepared=prepared))


def _render_mlody_value(
    value: MlodyValue,
    *,
    prepared: PreparedShowValue | None = None,
) -> RichDomNode:
    if isinstance(value, MlodyValueValue):
        if is_lineage_type(getattr(value.struct, "type", None)):
            lineage_rows = lineage_rows_from_payload(
                _prepared_display_payload(value, prepared)
            )
            if lineage_rows is not None:
                return panel(
                    _RichRenderableNode(build_lineage_console_table(lineage_rows)),
                    title="lineage",
                )
        payload = _prepared_display_payload(value, prepared)
        asset_payload = asset_metadata_payload(payload)
        if asset_payload is not None:
            return panel(
                _RichRenderableNode(build_asset_metadata_console_table(asset_payload)),
                title="asset",
            )
        raw_json = _raw_json_blob(payload, name=getattr(value.struct, "name", None))
        if raw_json is not None:
            return panel(SyntaxNode(raw_json, language="json"), title="value")
        if hasattr(payload, "as_mapping") or isinstance(payload, (list, dict)):
            content = _pretty_struct_str(payload)
        else:
            content = _format_value(payload)
        return panel(SyntaxNode(content, language="python"), title="value")
    return dom_for(value)


def _describe_mlody_value(value: MlodyValue) -> str:
    """Return a plain-text description of a value (for SQLite storage)."""
    if isinstance(value, MlodyVectorValue):
        return "\n".join(_describe_mlody_value(e) for e in value.elements)
    if isinstance(value, MlodyWorkspaceValue):
        return f"workspace: {value.name or '(cwd)'}\nroot: {value.root}"
    if isinstance(value, MlodyFolderValue):
        children = ", ".join(value.children) if value.children else "(empty)"
        return f"folder: {value.path}\nchildren: {children}"
    if isinstance(value, MlodySourceValue):
        return f"source: {value.path}.mlody"
    if isinstance(value, MlodyTaskValue):
        return f"task:\n{pretty_repr(value.struct)}"
    if isinstance(value, MlodyActionValue):
        return f"action:\n{pretty_repr(value.struct)}"
    if isinstance(value, MlodyValueValue):
        payload = _display_payload(value)
        raw_json = _raw_json_blob(payload, name=getattr(value.struct, "name", None))
        if raw_json is not None:
            return f"value:\n{raw_json}"
        if hasattr(payload, "as_mapping") or isinstance(payload, (list, dict)):
            return f"value:\n{_pretty_struct_str(payload)}"
        return f"value:\n{_format_value(payload)}"
    from mlody.resolver.values.internal import _RawAttrValue

    if isinstance(value, _RawAttrValue):
        return _format_value(value.value)
    return pretty_repr(value)


def _maybe_print_dag_plan(workspace: Workspace, label: str) -> None:
    try:
        dag = build_dag(workspace)
        selection = resolve_show_output_selection(dag, label)
        if selection is None or len(selection.graph.nodes) == 0:
            return
        render_dag_table(
            selection.graph,
            f"DAG — ancestors of '{label}'",
            console=_console,
        )
    except networkx.NetworkXUnfeasible:
        click.echo(
            click.style("Error: cycle detected in task graph", fg="red"), err=True
        )
    except Exception as exc:
        _logger.debug("Skipping DAG plan rendering for %r: %s", label, exc)


def _maybe_print_selected_dag_plan(selection: ActionGraphSelection, label: str) -> None:
    if selection.kind != "task-output" or len(selection.graph.nodes) == 0:
        return
    try:
        render_dag_table(
            selection.graph,
            f"DAG — ancestors of '{label}'",
            console=_console,
        )
    except networkx.NetworkXUnfeasible:
        click.echo(
            click.style("Error: cycle detected in task graph", fg="red"), err=True
        )
    except Exception as exc:
        _logger.debug("Skipping DAG plan rendering for %r: %s", label, exc)


def _selected_show_user(workspace: object, requested_user: str) -> str:
    evaluator = getattr(workspace, "evaluator", None)
    extra_ctx = getattr(evaluator, "_extra_ctx", None)
    workspace_ctx = getattr(extra_ctx, "workspace", None)
    selected_user = getattr(workspace_ctx, "user", None)
    if isinstance(selected_user, str):
        return selected_user
    return requested_user


@cli.command()
@click.option(
    "--with",
    "config",
    multiple=True,
    help="Extra value; may be repeated.",
)
@click.option(
    "--as",
    "run_as",
    default="mav",
    show_default=True,
    help="Registered user name or description for this show invocation.",
)
@click.argument("targets", nargs=-1, required=True)
@click.pass_context
def show(
    ctx: click.Context,
    config: list[str],
    run_as: str,
    targets: tuple[str, ...],
) -> None:
    """Resolve and display pipeline values.

    TARGETS: One or more Bazel-style target references. A target may be
    prefixed with a committoid and '|' separator (e.g. main|@root//pkg:tgt)
    to resolve against a specific commit rather than the current workspace.
    """
    # Support legacy test injection of a pre-built workspace via ctx.obj
    if "workspace" in ctx.obj:
        _show_with_legacy_workspace(ctx, config, run_as, targets)
        return

    monorepo_root: Path = ctx.obj["monorepo_root"]
    workspace_root: Path = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    has_error = False

    verbose: bool = ctx.obj.get("verbose", False)
    full_workspace: bool = ctx.obj.get("full_workspace", False)
    rendered_any_output = False

    for target in targets:
        try:
            workspace, resolved_sha = resolve_workspace(
                target,
                monorepo_root=monorepo_root,
                workspace_root=workspace_root,
                config=config,
                user=run_as,
                roots_file=roots,
                full_workspace=full_workspace,
                verbose=verbose,
            )
            selected_user = _selected_show_user(workspace, run_as)

            if resolved_sha is not None:
                _logger.debug("Resolved %s to %s", target.split("|")[0], resolved_sha)

            _committoid, inner_label = _parse_inner(target)
            for expanded_inner in workspace.expand_wildcard_label(inner_label):
                full_label = (
                    f"{_committoid}|{expanded_inner}" if _committoid else expanded_inner
                )
                if verbose:
                    click.echo(
                        json.dumps(
                            dataclasses.asdict(_parse_label_struct(full_label)),
                            indent=2,
                        )
                    )
                concrete_label = _concrete_show_label(_committoid, expanded_inner)
                execution = execute_show_action_graph(
                    workspace,
                    expanded_inner,
                    concrete_label,
                    resolve_label=resolve_label_to_value,
                )
                _maybe_print_selected_dag_plan(execution.selection, expanded_inner)
                mlody_value = execution.prepared_value.value

                if isinstance(mlody_value, MlodyUnresolvedValue):
                    has_error = True
                    click.echo(
                        click.style(f"Error: {mlody_value.reason}", fg="red"), err=True
                    )
                    continue

                effective_sha = resolved_sha or _head_sha_from_workspace(workspace)
                if effective_sha:
                    if resolved_sha is not None:
                        cache_root = Path.home() / _DEFAULT_WORKSPACES_SUFFIX
                        meta = _read_meta(cache_root, resolved_sha)
                        _rec_ref = str(meta.get("requested_ref", _committoid or target))
                        _rec_local_only = bool(meta.get("local_only", False))
                        _rec_repo = meta.get("repo") if isinstance(meta.get("repo"), str) else ""  # type: ignore[arg-type]
                        _rec_resolved_at = str(meta.get("resolved_at", datetime.now(timezone.utc).isoformat()))
                    else:
                        from mlody.resolver.git_client import GitClient  # noqa: PLC0415
                        _rec_ref = target
                        _rec_local_only = True
                        _rec_repo = GitClient(monorepo_root).remote_url() or ""
                        _rec_resolved_at = datetime.now(timezone.utc).isoformat()
                    from mlody.common.git_diff import local_changes  # noqa: PLC0415
                    patch, _ = local_changes(monorepo_root, effective_sha)
                    _record_evaluation(
                        resolved_sha=effective_sha,
                        requested_ref=_rec_ref,
                        local_only=_rec_local_only,
                        repo=_rec_repo,
                        resolved_at=_rec_resolved_at,
                        value_description=_describe_mlody_value(mlody_value),
                        patch=patch,
                    )

                if rendered_any_output or not _is_dag_value(mlody_value):
                    print()
                click.echo(f"Value for user '{selected_user}'")
                _error_sink: list[bool] = []
                _print_mlody_value(
                    mlody_value,
                    workspace=workspace,
                    _has_error=_error_sink,
                    prepared=execution.prepared_value,
                )
                rendered_any_output = True
                if _error_sink:
                    has_error = True
        except WorkspaceLoadError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue
        except WorkspaceResolutionError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue
        except KeyError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue
        except AttributeError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue

    if has_error:
        sys.exit(1)


def _show_with_legacy_workspace(
    ctx: click.Context,
    config: list[str],
    run_as: str,
    targets: tuple[str, ...],
) -> None:
    """Handle the legacy test injection path where ctx.obj['workspace'] is set.

    This path is used by existing tests that inject a pre-built workspace mock.
    It preserves backward compatibility for those tests.
    """
    workspace: Workspace = ctx.obj["workspace"]
    has_error = False
    workspace, selected_user = apply_workspace_user(
        workspace,
        run_as,
        resolved_sha=None,
    )

    try:
        workspace = configure_workspace(workspace, config)
    except WorkspaceResolutionError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        sys.exit(1)
    except KeyError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        sys.exit(1)
    except AttributeError as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
        sys.exit(1)

    for target in targets:
        try:
            _maybe_print_dag_plan(workspace, target)
            value = force(workspace.resolve(target))
        except KeyError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            available = list(workspace.root_infos.keys())
            if available:
                click.echo(
                    click.style(f"Available roots: {', '.join(available)}", fg="red"),
                    err=True,
                )
            continue
        except AttributeError as exc:
            has_error = True
            click.echo(click.style(f"Error: {exc}", fg="red"), err=True)
            continue

        click.echo(f"Value for user '{selected_user}'")
        click.echo(_format_value(value))

    if has_error:
        sys.exit(1)
