"""Command line entry: harvest metadata and write the CSV."""

from __future__ import annotations

import argparse
import asyncio
import csv
import shutil
import sys
from collections import Counter
from pathlib import Path

from scraper.from_csv import load_prefilled
from scraper.harvest import harvest_all
from scraper.import_page import harvest_url, prepare_url
from scraper.schema import COLUMNS

APP_CSV = Path("/Users/pkataria/Projects/RBC HackHub/data/black_history_metadata.csv")


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description=(
            "Harvest Dublin Core metadata into the project CSV. "
            "Pass a page URL, a prefilled CSV, or leave it empty to harvest the built-in collections."
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Page URL or a prefilled CSV. In a terminal you are asked when this is omitted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/black_history_metadata.csv"),
        help="Where to write the CSV inside this scraper project.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum records per collection. 0 harvests every listed record.",
    )
    parser.add_argument(
        "--only",
        choices=("all", "nsa", "extra"),
        default="all",
        help="all sources, only the Nova Scotia Archives exhibits, or only the later websites.",
    )
    parser.add_argument(
        "--copy-to",
        default=str(APP_CSV) if APP_CSV.parent.parent.exists() else "-",
        help="Also copy the CSV into the web app. Pass - to skip the copy.",
    )
    args = parser.parse_args(supplied)
    url = (args.url or "").strip()
    if not url and sys.stdin.isatty():
        print(
            "Enter a page URL, or the path to a prefilled CSV.\n"
            "A CSV is rewritten into the Dublin Core columns. Gemini reads the cells.\n"
            "Press Enter to harvest the built-in collections instead."
        )
        try:
            url = input("URL: ").strip()
        except EOFError:
            url = ""

    spreadsheet = _csv_path(url)
    if spreadsheet is not None and not spreadsheet.is_file():
        print(f"CSV not found: {spreadsheet}", file=sys.stderr)
        return 1

    if spreadsheet is not None:
        try:
            records, report = load_prefilled(spreadsheet, limit=args.limit)
        except (OSError, ValueError, csv.Error, RuntimeError) as exc:
            print(exc, file=sys.stderr)
            return 1
        print(report.text())
        explicit_copy = any(item == "--copy-to" or item.startswith("--copy-to=") for item in supplied)
        if not explicit_copy and args.copy_to != "-":
            print("Left the web app CSV unchanged. Pass --copy-to to copy this import.")
            args.copy_to = "-"
    elif url:
        try:
            url = prepare_url(url)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        records = asyncio.run(harvest_url(url, limit=args.limit))
        explicit_copy = any(item == "--copy-to" or item.startswith("--copy-to=") for item in supplied)
        if not explicit_copy and args.copy_to != "-":
            print("Left the web app CSV unchanged. Pass --copy-to to copy this import.")
            args.copy_to = "-"
    else:
        records = asyncio.run(harvest_all(limit_per_collection=args.limit, only=args.only))
    if not records:
        print("No records harvested.", file=sys.stderr)
        return 1

    _write_csv(args.output, records)
    print(f"Wrote {len(records)} records to {args.output.resolve()}")

    if args.copy_to and args.copy_to != "-":
        copy_to = Path(args.copy_to)
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, copy_to)
        print(f"Copied CSV into the web app at {copy_to}")

    counts = Counter(record["collection"] for record in records)
    for name, count in counts.items():
        print(f"  {count:4d}  {name}")
    return 0


def _csv_path(value: str) -> Path | None:
    if not value or not value.lower().endswith(".csv"):
        return None
    return Path(value).expanduser()


def _write_csv(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in COLUMNS})


if __name__ == "__main__":
    raise SystemExit(main())
