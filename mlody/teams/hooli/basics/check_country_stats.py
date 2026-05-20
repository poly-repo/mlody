"""CLI entrypoint for the check_country_stats task."""

from __future__ import annotations

import argparse
from pathlib import Path

from mlody.teams.hooli.basics.runtime import run_check_country_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the generated Hooli country stats table.")
    parser.add_argument("--input", required=True, help="Input CSV file")
    parser.add_argument("--output", required=True, help="Destination JSON report")
    args = parser.parse_args()
    run_check_country_stats(
        input_path=Path(args.input),
        output_path=Path(args.output),
    )


if __name__ == "__main__":
    main()
