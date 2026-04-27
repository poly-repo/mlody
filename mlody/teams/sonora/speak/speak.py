"""CLI entrypoint for Sonora Kokoro speech generation."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import click

from .runtime import (
    DEFAULT_MODEL_DIR,
    SpeakConfig,
    run_once,
    run_stdin,
)


@click.command()
@click.argument("text", required=False)
@click.option(
    "--file",
    "output_file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write WAV output to this path instead of live playback.",
)
@click.option(
    "--voice",
    type=str,
    default="af_heart",
    show_default=True,
    help="Voice name from model_dir/voices without .pt suffix.",
)
@click.option(
    "--lang-code",
    type=str,
    default="a",
    show_default=True,
    help="Kokoro language code.",
)
@click.option(
    "--speed",
    type=float,
    default=1.0,
    show_default=True,
    help="Synthesis speed multiplier.",
)
@click.option(
    "--sink",
    type=str,
    default=None,
    help="Optional Pulse sink/device name (paplay only).",
)
@click.option(
    "--model-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_MODEL_DIR,
    show_default=True,
    help="Local Kokoro model directory.",
)
def cli(
    *,
    text: str | None,
    output_file: Path | None,
    voice: str,
    lang_code: str,
    speed: float,
    sink: str | None,
    model_dir: Path,
) -> None:
    """Synthesize text to speech via local Kokoro model assets."""
    config = SpeakConfig(
        model_dir=model_dir,
        voice=voice,
        lang_code=lang_code,
        speed=speed,
        output_file=output_file,
        sink=sink,
    )
    try:
        if text is not None:
            run_once(config=config, text=text)
            return
        run_stdin(config=config, stream=sys.stdin)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


def main() -> None:
    """Execute the CLI."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
