"""CLI entrypoint for the change_model task."""

from __future__ import annotations

import argparse
from pathlib import Path

from mlody.teams.hooli.basics.runtime import run_change_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit a continent-level population change model.")
    parser.add_argument("--input", required=True, help="Input CSV file")
    parser.add_argument("--output", required=True, help="Destination JSON model artifact")
    args = parser.parse_args()
    run_change_model(
        input_path=Path(args.input),
        output_path=Path(args.output),
    )


if __name__ == "__main__":
    main()
