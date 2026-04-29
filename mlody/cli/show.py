"""show subcommand — resolve and display pipeline values."""

from __future__ import annotations

import dataclasses
from functools import singledispatch
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
from rich.console import Console
from rich.pretty import pretty_repr
from rich.table import Table

from common.python.console import RichDomNode, RichDomExecutor, SyntaxNode, panel

from mlody.cli.dag_render import render_dag_table, resolve_show_output_selection
from mlody.cli.main import cli
from mlody.core.dag import build_dag
from mlody.core.derived import DerivedValueShapeError
from mlody.core.sql.sql_query import MlodyQueryError
from mlody.core.tabular import (
    CsvSource,
    DerivedSource,
    MaterializedLocalSource,
    ParquetSource,
    PreviewResult,
    source_from_value,
)
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


def _display_payload(value: MlodyValueValue) -> object:
    """Return the printable payload for a resolved mlody value.

    Virtual values stay lazy through resolution, but the CLI should force them
    when the user explicitly asks to display them.
    """
    return force(value.struct)


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

        table = Table(title=header)
        for column_name in preview.column_names:
            table.add_column(column_name, overflow="fold")

        data_rows = preview.to_pydict()
        for i in range(preview.num_rows):
            table.add_row(
                *[
                    _cell_label(data_rows[column_name][i], image_encoder=image_encoder)
                    for column_name in preview.column_names
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
        fields = {
            k: v
            for k, v in obj.as_mapping().items()
            if not k.startswith("_")
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


@singledispatch
def _print_tabular_source(
    source: object,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
    """Render a tabular source when supported; return True on handled output."""
    _ = _has_error
    return False


@_print_tabular_source.register
def _(
    source: DerivedSource,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
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


@_print_tabular_source.register
def _(
    source: ParquetSource,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
    _ = _has_error
    try:
        _emit_tabular_preview(source.preview(50))
        return True
    except Exception:
        return False


@_print_tabular_source.register
def _(
    source: CsvSource,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
    _ = _has_error
    try:
        _emit_tabular_preview(source.preview(50))
        return True
    except Exception:
        return False


@_print_tabular_source.register
def _(
    source: MaterializedLocalSource,
    *,
    _has_error: list[bool] | None = None,
) -> bool:
    _ = _has_error
    try:
        _emit_tabular_preview(source.preview(50))
        return True
    except Exception:
        return False


def _print_mlody_value(
    value: MlodyValue, *, _has_error: list[bool] | None = None
) -> None:
    """Print a MlodyValue to the console.

    Data-backed values (parquet, derived) are rendered inline via click.echo.
    All structural values are rendered through the DOM via dom_executor.
    """
    dom_executor = RichDomExecutor(_console)

    if isinstance(value, MlodyVectorValue):
        for elem in value.elements:
            _print_mlody_value(elem, _has_error=_has_error)
        return

    if isinstance(value, MlodyValueValue):
        display_payload = _display_payload(value)
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
        if tabular_source is not None and _print_tabular_source(
            tabular_source,
            _has_error=_has_error,
        ):
            return

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

    dom_executor.render(_render_mlody_value(value))


def _render_mlody_value(value: MlodyValue) -> RichDomNode:
    if isinstance(value, MlodyValueValue):
        payload = _display_payload(value)
        if hasattr(payload, "as_mapping") or isinstance(payload, (list, dict)):
            content = _pretty_struct_str(payload)
        else:
            content = _format_value(payload)
        return panel(SyntaxNode(content, language="python"), title="value")
    return value.to_console_representation()


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
        if hasattr(payload, "as_mapping") or isinstance(payload, (list, dict)):
            return f"value:\n{_pretty_struct_str(payload)}"
        return f"value:\n{_format_value(payload)}"
    from mlody.resolver.label_value import _RawAttrValue

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
    workspace_root: Path = ctx.obj.get("workspace_root", monorepo_root)
    roots: Path | None = ctx.obj.get("roots")
    has_error = False

    verbose: bool = ctx.obj.get("verbose", False)
    full_workspace: bool = ctx.obj.get("full_workspace", False)

    for target in targets:
        try:
            workspace, resolved_sha = resolve_workspace(
                target,
                monorepo_root=monorepo_root,
                workspace_root=workspace_root,
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
                        value_description=_describe_mlody_value(mlody_value),
                    )

                print()
                _error_sink: list[bool] = []
                _print_mlody_value(mlody_value, _has_error=_error_sink)
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
