"""CLI entrypoint for the country_stats task."""

from __future__ import annotations

import argparse
from pathlib import Path

from mlody.teams.hooli.basics.runtime import COUNTRY_STATS_SOURCE_URL, run_country_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize Hooli country stats as CSV.")
    parser.add_argument("--source", default=COUNTRY_STATS_SOURCE_URL, help="HTTP URL or local HTML file")
    parser.add_argument("--output", required=True, help="Destination CSV file")
    args = parser.parse_args()
    run_country_stats(source=args.source, output_path=Path(args.output))


if __name__ == "__main__":
    main()
