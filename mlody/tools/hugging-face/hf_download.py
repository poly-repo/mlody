#!/usr/bin/env python3
"""Download a HuggingFace model to the local mlody artifact cache."""

import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

_CACHE_BASE = Path.home() / ".cache" / "mlody" / "artifacts" / "huggingface"


@click.command()
@click.argument("model_name")
def main(model_name: str) -> None:
    """Download MODEL_NAME (VENDOR/MODEL) to the local mlody cache."""
    if "/" not in model_name:
        console.print(
            f"[red]Error:[/red] model name must be VENDOR/MODEL, got: {model_name}"
        )
        sys.exit(1)

    vendor, model = model_name.split("/", 1)
    local_dir = _CACHE_BASE / vendor / model
    local_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"Downloading [bold]{model_name}[/bold] → {local_dir}")

    # Primary: huggingface_hub Python package
    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-untyped]
        from huggingface_hub.errors import (  # type: ignore[import-untyped]
            GatedRepoError,
            HfHubHTTPError,
        )

        try:
            snapshot_download(repo_id=model_name, local_dir=str(local_dir))
            console.print(f"[green]✓[/green] Downloaded {model_name}")
            return
        except (GatedRepoError, HfHubHTTPError) as exc:
            console.print(
                f"[yellow]huggingface_hub: {exc}[/yellow]\nFalling back to hf CLI…"
            )
    except ImportError:
        console.print("[yellow]huggingface_hub not installed, using hf CLI…[/yellow]")

    # Fallback: hf CLI tool
    result = subprocess.run(
        ["hf", "download", model_name, "--local-dir", str(local_dir)],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] hf download failed.")
        sys.exit(result.returncode)

    console.print(f"[green]✓[/green] Downloaded {model_name}")


if __name__ == "__main__":
    main()
