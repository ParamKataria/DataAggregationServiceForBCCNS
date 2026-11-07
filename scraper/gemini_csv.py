"""Ask Gemini to turn spreadsheet rows into Dublin Core fields.

The model sees the real headings and cell values. It does not use the heading
word list. The caller still checks that a link is a real URL and fills the
credit fields when the sheet itself does not name a publisher.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from scraper.schema import COLUMNS

_MODEL = "gemini-3.1-flash-lite"
_BATCH = 12
_WORKERS = 2

_INSTRUCTIONS = """You convert spreadsheet rows into Dublin Core metadata.

Use only words and dates that appear in that row's cells. Do not invent a fact, a place, a person, or a URL.
Ignore cells that are empty, n/a, or Not Stated.

For each input row, return one JSON object with these string fields and no others:
dc_title, dc_creator, dc_description, dc_subject, dc_date, dcterms_created, dcterms_issued, dc_type, dc_publisher, dc_identifier, dc_source, dc_coverage, dcterms_spatial, dcterms_temporal, dc_relation, dc_language, dc_format, dc_rights, dcterms_accessRights, dcterms_provenance, collection, media_url

How to fill them:
- dc_title: the person, document, or item the row is about. If the row has a first name and a last name, join them in normal reading order.
- dc_creator: the author, photographer, or maker only when a cell identifies one. Leave it empty when the row is a list of people and nobody is named as the author.
- dc_subject: rank, occupation, unit, or topics. Separate several values with " | ".
- dcterms_spatial: every real place in the row, separated with " | ". Include the city, the province or state, the country, the birthplace, and any other place column. A country of birth is a place. Do not leave a place only in the description, and do not put "Not Stated" here.
- dc_date: copy one date exactly as written. Do not reformat it and do not put a place in this field. Leave it empty when the row has no date.
- dcterms_temporal: a time span only when one is written, otherwise empty.
- dc_description: every remaining useful cell that is not a name, date, rank, or place, written as "Heading: value" and separated by spaces. Use a readable heading, so Kin rather than ARkin. Do not put a city, province, country, birthplace, or other place here.
- dc_identifier and media_url: a cell only when it is an http or https URL, otherwise empty.
- dc_type: Document for a register or nominal-roll entry. Use Photograph, Map, Newspaper, Biography, or Article only when the cells say that.
- dc_language: eng when the cells are English.
- Leave dc_publisher, dc_source, dc_rights, dcterms_accessRights, dcterms_provenance, collection, dc_coverage, dc_relation, dc_format, dcterms_created, and dcterms_issued empty.

Return a JSON array in the same order as the input rows, with one object per input row.
"""


def _load_env() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def transform_rows(
    fieldnames: list[str],
    rows: list[dict[str, str | None]],
    on_progress=None,
) -> list[dict[str, str]]:
    _load_env()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Add GEMINI_API_KEY to scraper/.env. A spreadsheet is read by Gemini.")
    model = os.environ.get("GEMINI_MODEL", _MODEL).strip() or _MODEL
    batches = [rows[start : start + _BATCH] for start in range(0, len(rows), _BATCH)]
    ordered: list[list[dict[str, str]] | None] = [None] * len(batches)
    done = 0
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_batch, key, model, fieldnames, batch, index): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            index = futures[future]
            ordered[index] = future.result()
            done += 1
            print(f"Gemini {done}/{len(batches)}", file=sys.stderr, flush=True)
            if on_progress is not None:
                on_progress(done, len(batches))
    records: list[dict[str, str]] = []
    for batch in ordered:
        if batch is None:
            raise RuntimeError("Gemini did not return every batch.")
        records.extend(batch)
    if len(records) != len(rows):
        raise RuntimeError(f"Gemini returned {len(records)} rows for {len(rows)} input rows.")
    return records


def _batch(
    key: str,
    model: str,
    fieldnames: list[str],
    rows: list[dict[str, str | None]],
    index: int,
) -> list[dict[str, str]]:
    payload = [{header: (row.get(header) or "").strip() for header in fieldnames} for row in rows]
    prompt = _INSTRUCTIONS + "\nInput rows:\n" + json.dumps(payload, ensure_ascii=False)
    last_error = "no response"
    for attempt in range(6):
        try:
            text = _generate(key, model, prompt)
            parsed = _parse_array(text)
            if len(parsed) != len(rows):
                raise RuntimeError(f"batch {index + 1} returned {len(parsed)} rows, expected {len(rows)}")
            return [_record(item, row) for item, row in zip(parsed, rows, strict=True)]
        except (RuntimeError, json.JSONDecodeError, httpx.HTTPError) as exc:
            last_error = str(exc)
            if any(code in last_error for code in ("returned 400", "returned 403", "returned 404")):
                break
            wait = 15 * (attempt + 1) if "returned 429" in last_error else 2 * (attempt + 1)
            print(f"Gemini batch {index + 1} retry in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Gemini could not read batch {index + 1}: {last_error}")


def _generate(key: str, model: str, prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {column: {"type": "STRING"} for column in COLUMNS},
                    "required": list(COLUMNS),
                },
            },
        },
    }
    response = httpx.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if response.status_code in {429, 500, 502, 503, 504}:
        raise RuntimeError(f"Gemini returned {response.status_code}")
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"Gemini returned {response.status_code}: {detail}")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no text: {json.dumps(data)[:400]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "\n".join(
        part.get("text", "") for part in parts if part.get("text") and not part.get("thought")
    )
    if not text.strip():
        raise RuntimeError("Gemini returned an empty response.")
    return text


def _parse_array(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < start:
            raise
        parsed = json.loads(text[start : end + 1])
    if isinstance(parsed, dict):
        for key in ("records", "rows", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list):
        raise json.JSONDecodeError("expected a list", text, 0)
    return parsed


def _record(item: dict, source: dict[str, str | None]) -> dict[str, str]:
    cells = " ".join((value or "") for value in source.values()).lower()
    record = {}
    for column in COLUMNS:
        value = item.get(column, "")
        if isinstance(value, list):
            value = " | ".join(str(part) for part in value)
        record[column] = str(value or "").strip()
    if record["dc_identifier"].lower() not in cells or not record["dc_identifier"].startswith(("http://", "https://")):
        record["dc_identifier"] = ""
    if not record["media_url"].startswith(("http://", "https://")) or record["media_url"].lower() not in cells:
        record["media_url"] = ""
    for column in ("dc_publisher", "dc_rights", "dc_source"):
        if record[column] and record[column].lower() not in cells:
            record[column] = ""
    return record
