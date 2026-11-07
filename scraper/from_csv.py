"""Turn any spreadsheet into the project's Dublin Core CSV.

Gemini reads the headings and the cell values and writes the Dublin Core
fields. This file then checks links, drops blank placeholders, and fills the
credit fields when the sheet does not name a publisher. The column order does
not change.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from scraper.gemini_csv import transform_rows
from scraper.schema import COLUMNS

_BLANK_VALUES = {"n/a", "na", "n.a", "n.a.", "none", "null", "nil", "not stated", "unknown", "-", "--"}
_LANG = {
    "en": "eng",
    "english": "eng",
    "fr": "fre",
    "french": "fre",
    "de": "ger",
    "german": "ger",
    "es": "spa",
    "spanish": "spa",
}


@dataclass
class CsvReport:
    source: Path
    row_count: int = 0
    filled: dict[str, int] = field(default_factory=dict)
    skipped: int = 0

    def text(self) -> str:
        lines = [
            f"Spreadsheet {self.source.name}: Gemini read {self.row_count} rows and wrote Dublin Core fields from the cell values.",
        ]
        if self.filled:
            lines.append("Filled only where the cell was empty, so the source can be credited:")
            for column, count in self.filled.items():
                lines.append(f"  {column}: {count} rows")
        if self.skipped:
            lines.append(f"Skipped {self.skipped} empty rows.")
        return "\n".join(lines)


def load_prefilled(path: Path, limit: int = 0, on_progress=None) -> tuple[list[dict[str, str]], CsvReport]:
    path = path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row.")
        fieldnames = [header for header in reader.fieldnames if header and _key(header)]
        raw_rows = []
        for raw in reader:
            if any(_useful(value or "") for value in raw.values()):
                raw_rows.append(raw)
    if limit > 0:
        raw_rows = raw_rows[:limit]
    if not raw_rows:
        return [], CsvReport(source=path)

    drafted = transform_rows(fieldnames, raw_rows, on_progress=on_progress)
    report = CsvReport(source=path, row_count=len(raw_rows))
    records = []
    retrieved_on = date.today().isoformat()
    sheet_name = _sheet_name(path)
    for raw, draft in zip(raw_rows, drafted, strict=True):
        record, filled = _credit(draft, raw, path.name, sheet_name, retrieved_on)
        if record is None:
            report.skipped += 1
            continue
        for column in filled:
            report.filled[column] = report.filled.get(column, 0) + 1
        records.append(record)
    return records, report


def _credit(
    draft: dict[str, str],
    raw: dict[str, str | None],
    filename: str,
    sheet_name: str,
    retrieved_on: str,
) -> tuple[dict[str, str] | None, list[str]]:
    row = {column: _useful(draft.get(column, "")) for column in COLUMNS}
    row["dc_language"] = _language(row["dc_language"])
    filled: list[str] = []

    def fill(column: str, value: str) -> None:
        if row[column] or not value:
            return
        row[column] = value
        filled.append(column)

    if not row["dc_title"]:
        for column in ("dc_description", "dc_subject", "dc_date", "dcterms_spatial", "dc_identifier"):
            if row[column]:
                row["dc_title"] = row[column][:120]
                filled.append("dc_title")
                break
    if not row["dc_title"] and not any(row[column] for column in COLUMNS):
        return None, []
    if not row["dc_title"]:
        fallback = next((_useful(value or "") for value in raw.values() if _useful(value or "")), "")
        if not fallback:
            return None, []
        row["dc_title"] = fallback[:120]
        filled.append("dc_title")

    publisher = row["dc_publisher"] or _host(row["dc_identifier"]) or sheet_name
    fill("dc_publisher", publisher)
    publisher = row["dc_publisher"]
    if row["dc_identifier"]:
        fill("dc_source", f"{publisher} ({row['dc_identifier']})")
    else:
        fill("dc_source", f"{sheet_name} ({filename})")
    fill("dc_rights", f"Rights remain with {publisher}.")
    fill(
        "dcterms_accessRights",
        "Metadata only. The publisher named in dc_publisher decides access to the work.",
    )
    fill("dc_type", "Document")
    fill("collection", sheet_name)
    fill("dc_relation", _origin(row["dc_identifier"]))
    if not row["dc_coverage"]:
        fill(
            "dc_coverage",
            "; ".join(part for part in (row["dcterms_spatial"], row["dcterms_temporal"]) if part),
        )
    fill(
        "dcterms_provenance",
        (
            f"Taken on {retrieved_on} from the supplied spreadsheet {filename}. "
            "Gemini placed each field from the cell values. The work was not copied."
        ),
    )
    return row, filled


def _language(value: str) -> str:
    if not value:
        return ""
    pieces = value.split("|") if "|" in value else [value]
    mapped: list[str] = []
    for piece in pieces:
        part = _useful(piece)
        if not part:
            continue
        key = part.lower().replace("_", "-")
        primary = key.split("-")[0]
        mapped_part = _LANG.get(key, _LANG.get(primary, part if len(part) == 3 else key))
        if mapped_part and mapped_part not in mapped:
            mapped.append(mapped_part)
    return " | ".join(mapped)


def _sheet_name(path: Path) -> str:
    stem = re.sub(r"[_\s-]*\d{8}$", "", path.stem)
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem or path.stem


def _useful(value: str) -> str:
    cleaned = _clean(value)
    if cleaned.lower() in _BLANK_VALUES:
        return ""
    return cleaned


def _host(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _origin(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    parts = urlsplit(url)
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def _clean(value: str) -> str:
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
