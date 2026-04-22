"""show subcommand — resolve and display pipeline values."""

from __future__ import annotations

import dataclasses
import json
import logging
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
import pyarrow.parquet as pq
from rich.console import Console
from rich.pretty import pretty_repr
from rich.syntax import Syntax
from rich.table import Table

from mlody.cli.main import cli
from mlody.core.dag import Edge, TaskNode, ancestors_subgraph, build_dag
from mlody.core.derived import DerivedValueShapeError, materialise_derived
from mlody.core.sql.sql_query import MlodyQueryError, mlody_query
from mlody.core.targets import parse_target
from mlody.core.workspace import Workspace, WorkspaceLoadError, force
from mlody.db.evaluations import open_db, write_evaluation
from mlody.db.local_diff import compute_local_diff_sha, get_repo_root
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
    resolve_label_to_value,
    resolve_workspace,
)
from mlody.resolver.errors import WorkspaceResolutionError

_logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SUFFIX = Path(".cache") / "mlody"
_DEFAULT_DB_NAME = "mlody.sqlite"
_DEFAULT_WORKSPACES_SUFFIX = _DEFAULT_CACHE_SUFFIX / "workspaces"
_console = Console()


def _get_username() -> str:
    """Return the OS username; falls back to pwd lookup if os.getlogin() raises."""
    try:
        return os.getlogin()
    except OSError:
        return pwd.getpwuid(os.getuid()).pw_name


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
) -> None:
    """Write one evaluation row to the local SQLite database.

    Best-effort: logs at ERROR level and returns on any failure so a DB error
    never terminates the show command (NFR-AVAIL-001: never a silent crash —
    the error is logged clearly). Connection is always closed in the finally
    block.
    """
    db_path = Path.home() / _DEFAULT_CACHE_SUFFIX / _DEFAULT_DB_NAME
    conn = None
    try:
        conn = open_db(db_path)
        local_diff_sha = compute_local_diff_sha(get_repo_root())
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
        )
    except Exception as exc:
        _logger.error("Failed to write evaluation to %s: %s", db_path, exc)
    finally:
        if conn is not None:
            conn.close()


def show_fn(
    label: str,
    monorepo_root: Path,
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
        roots_file=roots_file,
        full_workspace=full_workspace,
        print_fn=print_fn,
        verbose=verbose,
    )
    _committoid, inner_label = _parse_inner(label)
    print_fn(pretty_repr(_parse_label_struct(label)))

    from mlody.core.label import parse_label as _core_parse_label

    concrete_label = _core_parse_label(inner_label)
    mlody_value = resolve_label_to_value(concrete_label, workspace)
    return mlody_value


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
        if not (raw[:4] in (b"\x89PNG", b"GIF8", b"RIFF", b"BM\x00\x00") or raw[:2] == b"\xff\xd8"):
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


def _kitty_encode(img, *, max_width: int = 640) -> str | None:  # img: PIL.Image.Image
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

        buf = _io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=False)
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
                parts.append(f"\x1b_Ga=T,f=100,q=1,m={more};{chunk}\x1b\\")
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
        pal = q.getpalette()          # flat [R, G, B, …]; length = num_used_colors × 3
        num_colors = len(pal) // 3
        pixels = q.load()             # PixelAccess: pixels[x, y] → palette index

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
                    parts.append("$")   # carriage-return: restart at column 0
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
            parts.append("-")           # advance to next sixel band

        parts.append("\x1b\\")          # DCS string terminator
        return "".join(parts)
    except Exception:
        return None


def _cell_label(value: object, *, image_encoder=None) -> str:
    """Return a displayable string for one table cell.

    If *image_encoder* is provided and the value is image data, the encoder is
    called with the decoded PIL Image and its return value is used directly
    (embedding the terminal escape sequence inline).  Falls back to a compact
    text label when no encoder is given or encoding fails.
    """
    if isinstance(value, dict) and isinstance(value.get("bytes"), bytes):
        img = _to_pil_image(value)
        if img is not None:
            if image_encoder is not None:
                try:
                    encoded = image_encoder(img)
                    if encoded:
                        return encoded
                except Exception:
                    pass
            return f"<{img.format or 'image'} {img.width}×{img.height}>"
        nb = len(value["bytes"])
        return f"<image {nb} bytes>"
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    return str(value)


def _image_encoder_for_terminal():
    """Return an image encoder callable for the current terminal, or None."""
    if _can_kitty():
        return lambda img: _kitty_encode(img, max_width=160)
    if _can_sixel():
        return lambda img: _sixel_encode(img, max_width=80)
    return None


def _format_value(value: object, *, total_rows: int | None = None, image_encoder=None) -> str:
    try:
        import pyarrow as pa  # noqa: PLC0415

        if isinstance(value, pa.Table):
            rows = value.num_rows
            display_total = total_rows if total_rows is not None else rows
            cols = value.num_columns
            header = f"pyarrow.Table  {display_total} rows × {cols} columns"
            # Truncate to first 50 rows so the terminal doesn't flood.
            preview = value.slice(0, 50)
            col_names = preview.column_names
            data_rows = preview.to_pydict()
            lines: list[str] = [", ".join(col_names)]
            for i in range(preview.num_rows):
                lines.append(
                    ", ".join(
                        _cell_label(data_rows[c][i], image_encoder=image_encoder)
                        for c in col_names
                    )
                )
            body = "\n".join(lines)
            if display_total > rows:
                body += f"\n… ({display_total - rows} more rows not shown)"
            return f"{header}\n{body}"
    except ImportError:
        pass
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
        fields = {
            k: v
            for k, v in obj.as_mapping().items()
            if not k.startswith("_") or k == "_source_range"
        }
        if not fields:
            return "struct()"
        parts = [f"{inner}{k}={_pretty_struct_str(v, _depth + 1)}" for k, v in fields.items()]
        return "struct(\n" + ",\n".join(parts) + f",\n{pad})"

    if isinstance(obj, list):
        if not obj:
            return "[]"
        parts = [f"{inner}{_pretty_struct_str(v, _depth + 1)}" for v in obj]
        return "[\n" + ",\n".join(parts) + f",\n{pad}]"

    if isinstance(obj, dict):
        if not obj:
            return "{}"
        parts = [f"{inner}{k!r}: {_pretty_struct_str(v, _depth + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(parts) + f",\n{pad}}}"

    if callable(obj) and not isinstance(obj, type):
        return "<callable>"

    return repr(obj)


def _source_paths_from_location(location: object) -> str | list[str] | None:
    """Extract the file path(s) from a posix location struct.

    Checks both the top-level ``path`` field (used by composed locations
    produced by ``_posix_compose``) and the nested ``attributes["path"]`` field
    (used by factory-created locations from ``extend_attrs``).

    Returns a string path, list of paths, or None if no path is found.
    """
    if location is None:
        return None

    def _coerce(path: object) -> str | list[str]:
        if isinstance(path, list):
            return [str(p) for p in path]
        return str(path)

    # Composed locations (from _posix_compose) store path as a direct field.
    direct = getattr(location, "path", None)
    if direct is not None:
        return _coerce(direct)

    # Factory-created locations (from extend_attrs) store path inside attributes.
    attrs: dict[str, object] | None = getattr(location, "attributes", None)
    if isinstance(attrs, dict):
        path = attrs.get("path")
        if path is not None:
            return _coerce(path)

    return None


def _print_row_list(rows: list, *, image_encoder=None) -> None:
    """Display a list of row dicts from parquet traversal with inline image support."""
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            click.echo(repr(row))
            continue
        click.echo(f"[{i}]")
        for k, v in row.items():
            click.echo(f"  {k}: {_cell_label(v, image_encoder=image_encoder)}")


def _print_mlody_value(value: MlodyValue, *, _has_error: list[bool] | None = None) -> None:
    """Print a MlodyValue to the console with syntax highlighting.

    For ``MlodyValueValue`` instances with a ``derived`` location, materialises
    the derived value first.  Any ``DerivedValueShapeError`` or
    ``MlodyQueryError`` is caught, displayed in red, and the ``_has_error`` flag
    is set so the caller can exit with code 1.
    """
    if isinstance(value, MlodyVectorValue):
        # Render each element in the vector using the existing per-kind dispatchers.
        # Elements are printed sequentially; an empty vector produces no output.
        for elem in value.elements:
            _print_mlody_value(elem, _has_error=_has_error)
        return
    if isinstance(value, MlodyValueValue):
        location = getattr(value.struct, "location", None)
        loc_type = None
        if location is not None:
            loc_type = getattr(location, "type", None)

        if loc_type == "derived":
            # Materialise the derived value and render the resulting table.
            attrs: dict[str, object] = getattr(location, "attributes", {})  # type: ignore[assignment]
            source_ref = attrs.get("source_ref", "")
            # Prefer pre-resolved source_paths stored in attributes (populated
            # during construction in values.mlody and updated by _derived_compose
            # during field traversal).
            source_paths_attr = attrs.get("source_paths")
            if source_paths_attr and isinstance(source_paths_attr, list):
                source_paths: str | list[str] = source_paths_attr
            else:
                # Fall back: resolve from the source value's location struct.
                source_struct = getattr(value.struct, "source", None)
                source_location = getattr(source_struct, "location", None) if source_struct else None
                source_paths = _source_paths_from_location(source_location)
                # Final fallback to source_ref string.
                if source_paths is None:
                    source_paths = str(source_ref)
            try:
                output_path = materialise_derived(location, source_paths)
                table: pa.Table = pq.read_table(output_path)
                click.echo(_format_value(table, image_encoder=_image_encoder_for_terminal()))
            except DerivedValueShapeError as exc:
                click.echo(
                    click.style(f"Error: derived query produced a scalar result — {exc}", fg="red"),
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
            return

        # For any location with resolvable paths, attempt to read as parquet
        # using mlody_query (DuckDB) which handles globs natively.
        # Directories are converted to recursive globs so DuckDB can find
        # nested parquet files. Falls through silently to struct display if
        # the location is not parquet-backed or the files are absent.
        if location is not None:
            raw_paths = _source_paths_from_location(location)
            if raw_paths is not None:

                def _parquet_path(p: str) -> str:
                    ep = os.path.expanduser(p)
                    return ep + "/**/*.parquet" if os.path.isdir(ep) else ep

                if isinstance(raw_paths, list):
                    coerced = [_parquet_path(p) for p in raw_paths]
                    # Unwrap single-element list so DuckDB treats it as a glob
                    query_paths: str | list[str] = coerced[0] if len(coerced) == 1 else coerced
                else:
                    query_paths = _parquet_path(raw_paths)

                try:
                    count_table = mlody_query(query_paths, "SELECT COUNT(*) as n")
                    total_rows = int(count_table.column("n")[0].as_py())
                    table = mlody_query(query_paths, "SELECT * LIMIT 50")
                    enc = _image_encoder_for_terminal()
                    click.echo(_format_value(table, total_rows=total_rows, image_encoder=enc))
                    return
                except Exception:
                    pass  # not parquet or unreadable — fall through

        _console.print("value:")
        _console.print(Syntax(_pretty_struct_str(value.struct), "python", theme="monokai", word_wrap=True))
        return
    if isinstance(value, MlodyTaskValue):
        _console.print("task:")
        _console.print(Syntax(_pretty_struct_str(value.struct), "python", theme="monokai", word_wrap=True))
        return
    if isinstance(value, MlodyActionValue):
        _console.print("action:")
        _console.print(Syntax(_pretty_struct_str(value.struct), "python", theme="monokai", word_wrap=True))
        return
    # _RawAttrValue wrapping a pa.Table or list-of-dicts (from parquet traversal
    # or [@sql …] entity query) — render with full image support.
    from mlody.resolver.label_value import _RawAttrValue  # noqa: PLC0415

    if isinstance(value, _RawAttrValue):
        enc = _image_encoder_for_terminal()
        if isinstance(value.value, pa.Table):
            click.echo(_format_value(value.value, image_encoder=enc))
            return
        if isinstance(value.value, list):
            _print_row_list(value.value, image_encoder=enc)
            return
        if isinstance(value.value, dict):
            _print_row_list([value.value], image_encoder=enc)
            return
    click.echo(_render_mlody_value(value))


def _render_mlody_value(value: MlodyValue) -> str:
    """Render a typed MlodyValue to a human-readable string for stdout.

    Each branch corresponds to a value kind. The exact format is an
    implementation-time detail (design Q-01): using str()/pretty_repr()
    for now to produce sensible output for all types.
    """
    if isinstance(value, MlodyVectorValue):
        # Render each element separated by newlines; empty vector → empty string.
        parts = [_render_mlody_value(elem) for elem in value.elements]
        return "\n".join(parts)
    if isinstance(value, MlodyWorkspaceValue):
        name = value.name or "(cwd)"
        return f"workspace: {name}\nroot: {value.root}"
    if isinstance(value, MlodyFolderValue):
        children_display = ", ".join(value.children) if value.children else "(empty)"
        return f"folder: {value.path}\nchildren: {children_display}"
    if isinstance(value, MlodySourceValue):
        return f"source: {value.path}.mlody"
    if isinstance(value, MlodyTaskValue):
        return f"task:\n{pretty_repr(value.struct)}"
    if isinstance(value, MlodyActionValue):
        return f"action:\n{pretty_repr(value.struct)}"
    if isinstance(value, MlodyValueValue):
        return f"value:\n{pretty_repr(value.struct)}"
    # _RawAttrValue — terminal attribute reached after traversal
    from mlody.resolver.label_value import _RawAttrValue

    if isinstance(value, _RawAttrValue):
        return _format_value(value.value)
    # MlodyUnresolvedValue is handled by the caller (exits 1), not here
    return pretty_repr(value)


def _short_type_name(value: object) -> str:
    t = getattr(value, "type", None)
    if t is None:
        return "?"
    t_name = getattr(t, "name", None)
    if isinstance(t_name, str) and t_name:
        return t_name
    if isinstance(t, str) and t:
        return t
    return "?"


def _format_value_list(values: object) -> str:
    if not isinstance(values, list) or not values:
        return "—"
    rendered: list[str] = []
    for v in values:
        name = getattr(v, "name", None)
        if not isinstance(name, str) or not name:
            name = str(v)
        rendered.append(f"{name}:{_short_type_name(v)}")
    return ", ".join(rendered)


def _format_action_cell(action_obj: object, fallback_name: str) -> str:
    if action_obj is None:
        return fallback_name
    name = getattr(action_obj, "name", None)
    if not isinstance(name, str) or not name:
        name = fallback_name
    a_inputs = _format_value_list(getattr(action_obj, "inputs", []))
    a_outputs = _format_value_list(getattr(action_obj, "outputs", []))
    a_config = _format_value_list(getattr(action_obj, "config", []))
    return f"{name}\nAIn:  {a_inputs}\nAOut: {a_outputs}\nACfg: {a_config}"


def _render_dag_table(display_graph: networkx.MultiDiGraph, title: str) -> None:
    try:
        order = list(networkx.topological_sort(display_graph))
    except networkx.NetworkXUnfeasible:
        click.echo(
            click.style("Error: cycle detected in task graph", fg="red"), err=True
        )
        return

    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Task", style="cyan", no_wrap=True, ratio=4)
    table.add_column("Action", style="magenta", no_wrap=False, ratio=2)
    table.add_column("Dependencies", style="white", ratio=5)

    for node_id in order:
        task_node = display_graph.nodes[node_id]["task"]
        task_struct = display_graph.nodes[node_id]["task_struct"]
        deps: list[str] = []
        for src_id, _, data in display_graph.in_edges(node_id, data=True):
            edge: Edge = data["edge"]
            deps.append(f"{src_id}\n  {edge.src_port} → {edge.dst_path}")
        inputs_str = _format_value_list(getattr(task_struct, "inputs", []))
        outputs_str = _format_value_list(getattr(task_struct, "outputs", []))
        config_str = _format_value_list(getattr(task_struct, "config", []))
        task_cell = (
            f"{node_id}\nIn:  {inputs_str}\nOut: {outputs_str}\nCfg: {config_str}"
        )
        table.add_row(
            task_cell,
            _format_action_cell(getattr(task_struct, "action", None), task_node.action),
            "\n\n".join(deps) if deps else "—",
        )

    _console.print(table)


def _subgraph_for_show_output_label(
    dag: networkx.MultiDiGraph, label: str
) -> networkx.MultiDiGraph | None:
    try:
        addr = parse_target(label)
    except ValueError:
        return None
    if len(addr.field_path) == 2 and addr.field_path[0] == "outputs":
        return ancestors_subgraph(dag, addr.field_path[1])
    return None


def _maybe_print_dag_plan(workspace: Workspace, label: str) -> None:
    try:
        dag = build_dag(workspace)
        subgraph = _subgraph_for_show_output_label(dag, label)
        if subgraph is None or len(subgraph.nodes) == 0:
            return
        _render_dag_table(subgraph, f"DAG — ancestors of '{label}'")
    except Exception as exc:
        _logger.debug("Skipping DAG plan rendering for %r: %s", label, exc)


@cli.command()
@click.argument("targets", nargs=-1, required=True)
@click.pass_context
def show(ctx: click.Context, targets: tuple[str, ...]) -> None:
    """Resolve and display pipeline values.

    TARGETS: One or more Bazel-style target references. A target may be
    prefixed with a committoid and '|' separator (e.g. main|@root//pkg:tgt)
    to resolve against a specific commit rather than the current workspace.
    """
    # Support legacy test injection of a pre-built workspace via ctx.obj
    if "workspace" in ctx.obj:
        _show_with_legacy_workspace(ctx, targets)
        return

    monorepo_root: Path = ctx.obj["monorepo_root"]
    roots: Path | None = ctx.obj.get("roots")
    has_error = False

    verbose: bool = ctx.obj.get("verbose", False)
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    for target in targets:
        try:
            workspace, resolved_sha = resolve_workspace(
                target,
                monorepo_root=monorepo_root,
                roots_file=roots,
                full_workspace=full_workspace,
                verbose=verbose,
            )
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
                _maybe_print_dag_plan(workspace, expanded_inner)

                # Resolve the concrete label to a typed MlodyValue (new pipeline step)
                from mlody.core.label import parse_label as _core_parse_label
                from mlody.core.label.label import Label as _Label

                if expanded_inner == "":
                    # Bare workspace label (e.g. "HEAD", "main") — construct
                    # the label directly rather than parsing an empty string.
                    concrete_label = _Label(
                        workspace=_committoid,
                        workspace_query=None,
                        entity=None,
                        entity_query=None,
                        attribute_path=None,
                        attribute_query=None,
                    )
                else:
                    concrete_label = _core_parse_label(expanded_inner)
                mlody_value = resolve_label_to_value(concrete_label, workspace)

                if isinstance(mlody_value, MlodyUnresolvedValue):
                    has_error = True
                    click.echo(
                        click.style(f"Error: {mlody_value.reason}", fg="red"), err=True
                    )
                    continue

                if resolved_sha is not None:
                    # Only record evaluations for committoid-qualified labels
                    # (cwd-relative labels have no resolved_sha).
                    cache_root = Path.home() / _DEFAULT_WORKSPACES_SUFFIX
                    meta = _read_meta(cache_root, resolved_sha)
                    _record_evaluation(
                        resolved_sha=resolved_sha,
                        requested_ref=str(
                            meta.get("requested_ref", _committoid or target)
                        ),
                        local_only=bool(meta.get("local_only", False)),
                        repo=meta.get("repo")
                        if isinstance(meta.get("repo"), str)
                        else "",  # type: ignore[arg-type]
                        resolved_at=str(
                            meta.get(
                                "resolved_at", datetime.now(timezone.utc).isoformat()
                            )
                        ),
                        value_description=_render_mlody_value(mlody_value),
                    )

                print("-------------------------------")
                _error_sink: list[bool] = []
                _print_mlody_value(mlody_value, _has_error=_error_sink)
                if _error_sink:
                    has_error = True
                print("-------------------------------")
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


def _show_with_legacy_workspace(ctx: click.Context, targets: tuple[str, ...]) -> None:
    """Handle the legacy test injection path where ctx.obj['workspace'] is set.

    This path is used by existing tests that inject a pre-built workspace mock.
    It preserves backward compatibility for those tests.
    """
    workspace: Workspace = ctx.obj["workspace"]
    has_error = False

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

        click.echo(_format_value(value))

    if has_error:
        sys.exit(1)
