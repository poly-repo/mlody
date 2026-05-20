"""CLI entrypoint for the continent_stats task."""

from __future__ import annotations

import argparse
from pathlib import Path

from mlody.teams.hooli.basics.runtime import run_continent_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate Hooli country stats by continent.")
    parser.add_argument("--input", required=True, help="Input country stats CSV file")
    parser.add_argument("--model", required=True, help="Input JSON model artifact")
    parser.add_argument("--output", required=True, help="Destination CSV file")
    args = parser.parse_args()
    run_continent_stats(
        input_path=Path(args.input),
        model_path=Path(args.model),
        output_path=Path(args.output),
    )


if __name__ == "__main__":
    main()
