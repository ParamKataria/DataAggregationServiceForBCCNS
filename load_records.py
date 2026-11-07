"""Load black_history_metadata.csv into the existing Supabase tables.

Uses the service-role key. Does not create, drop, or truncate tables.
Upserts so a second run updates the same rows. search_vector is left unset
so the existing trigger can fill it.

Run from this folder:

    uv run python load_records.py
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPECTED_COUNTS = {
    "Nova Scotia Archives": 747,
    "The Canadian Encyclopedia": 196,
    "Black Loyalist Heritage Centre": 176,
    "A Black People's History of Canada": 36,
    "Library and Archives Canada": 11,
}
TYPE_MAP = {
    "map": "map",
    "document": "document",
    "photograph": "photograph",
    "newspaper": "newspaper",
    "oral history": "oral history",
    "artifact": "artifact",
    "audio": "audio",
    "video": "video",
    "biography": "biography",
    "article": "article",
    "educational resource": "educational resource",
    "research guide": "research guide",
    "poster": "poster",
    "timeline": "timeline",
    "magazine": "magazine",
}
TEXT_COLUMNS = (
    "dc_title",
    "dc_description",
    "dc_creator",
    "dc_rights",
    "dcterms_provenance",
    "dcterms_spatial",
    "dcterms_temporal",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upsert the Dublin Core CSV into Supabase.")
    parser.add_argument("csv", nargs="?", help="Path to black_history_metadata.csv.")
    args = parser.parse_args(argv)

    load_env()
    url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        print("Need NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SECRET_KEY in scraper/.env.", file=sys.stderr)
        return 1
    assert_service_role(key)

    csv_path = find_csv(args.csv)
    prepared = prepare_rows(read_csv(csv_path))
    print(f"Read {len(prepared)} rows from {csv_path}")

    client = Supabase(url, key)
    source_ids = client.upsert_names("sources", {row["dc_publisher"] for row in prepared})
    person_ids = client.upsert_names("people", {name for row in prepared for name in row["people"]})
    subject_ids = client.upsert_names("subjects", {name for row in prepared for name in row["subjects"]})
    place_ids = client.upsert_names("places", {name for row in prepared for name in row["places"]})

    records = []
    for row in prepared:
        records.append(
            {
                "source_id": source_ids[row["dc_publisher"]],
                "dc_title": row["dc_title"],
                "dc_identifier": row["dc_identifier"],
                "dc_publisher": row["dc_publisher"],
                "dc_source": row["dc_source"],
                "dc_type": row["dc_type"],
                "dc_description": row["dc_description"],
                "dc_creator": row["dc_creator"],
                "dc_subject": row["subjects"],
                "dc_date": row["dc_date"],
                "date_text": row["date_text"],
                "dcterms_spatial": row["dcterms_spatial"],
                "dcterms_temporal": row["dcterms_temporal"],
                "dc_rights": row["dc_rights"],
                "dcterms_access_rights": row["dcterms_access_rights"],
                "dcterms_provenance": row["dcterms_provenance"],
                "bccns_community": [],
                "bccns_access_status": row["bccns_access_status"],
                "bccns_digitized": row["bccns_digitized"],
                "raw_metadata": row["raw_metadata"],
            }
        )
    saved = client.upsert("records", records, "dc_identifier", "id,dc_identifier")
    record_ids = {row["dc_identifier"]: row["id"] for row in saved}

    people_links = []
    subject_links = []
    place_links = []
    desired_people: dict[str, set[str]] = {}
    desired_subjects: dict[str, set[str]] = {}
    desired_places: dict[str, set[str]] = {}
    for row in prepared:
        record_id = record_ids[row["dc_identifier"]]
        person_set = {person_ids[name] for name in row["people"]}
        subject_set = {subject_ids[name] for name in row["subjects"]}
        place_set = {place_ids[name] for name in row["places"]}
        desired_people[record_id] = person_set
        desired_subjects[record_id] = subject_set
        desired_places[record_id] = place_set
        people_links.extend(
            {"record_id": record_id, "person_id": person_id, "role": "creator"} for person_id in person_set
        )
        subject_links.extend(
            {"record_id": record_id, "subject_id": subject_id} for subject_id in subject_set
        )
        place_links.extend({"record_id": record_id, "place_id": place_id} for place_id in place_set)

    client.upsert("record_people", people_links, "record_id,person_id,role", "record_id")
    client.upsert("record_subjects", subject_links, "record_id,subject_id", "record_id")
    client.upsert("record_places", place_links, "record_id,place_id", "record_id")
    removed = client.remove_stale_links(desired_people, desired_subjects, desired_places)
    removed_names = client.remove_unlinked_combined_names()

    counts = client.publisher_counts()
    print("Publisher counts:")
    for name, expected in EXPECTED_COUNTS.items():
        actual = counts.get(name, 0)
        print(f"  {actual:4d}  {name}")
        if actual != expected:
            print(f"Expected {expected} for {name}.", file=sys.stderr)
            return 1
    extras = {name: count for name, count in counts.items() if name not in EXPECTED_COUNTS}
    if extras:
        print(f"Unexpected publishers: {extras}", file=sys.stderr)
        return 1
    if removed or removed_names:
        print(
            f"Synced links. Removed {removed} outdated link rows "
            f"and {removed_names} unused combined creator names."
        )
    print(f"Upserted {len(saved)} records. search_vector was left to the database trigger.")
    return 0


def prepare_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    prepared = []
    used: set[str] = set()
    seen_original: set[str] = set()
    for raw in rows:
        original = raw.get("dc_identifier", "").strip()
        if not original:
            raise SystemExit("A row is missing dc_identifier.")
        if original not in seen_original:
            seen_original.add(original)
            identifier = original
        else:
            identifier = unique_identifier(f"{original}#{slugify(raw.get('dc_title', ''))}", used)
        if identifier in used:
            identifier = unique_identifier(identifier, used)
        used.add(identifier)

        publisher = raw.get("dc_publisher", "").strip()
        if not publisher:
            raise SystemExit(f"Missing publisher for {original}")
        title = raw.get("dc_title", "").strip()
        if not title:
            raise SystemExit(f"Missing title for {original}")
        resource_type = TYPE_MAP.get(raw.get("dc_type", "").strip().lower())
        if resource_type is None:
            raise SystemExit(f"Unknown dc_type {raw.get('dc_type')!r} for {original}")

        source = raw.get("dc_source", "").strip()
        if not source:
            source = publisher
        date_value = raw.get("dc_date", "").strip()
        if ISO_DATE.fullmatch(date_value):
            dc_date = date_value
            date_text = None
        elif date_value:
            dc_date = None
            date_text = date_value
        else:
            dc_date = None
            date_text = None
        access = raw.get("dcterms_accessRights", "")
        prepared.append(
            {
                "dc_identifier": identifier,
                "dc_title": title,
                "dc_publisher": publisher,
                "dc_source": source,
                "dc_type": resource_type,
                "dc_description": blank(raw.get("dc_description", "")),
                "dc_creator": blank(raw.get("dc_creator", "")),
                "dc_rights": blank(raw.get("dc_rights", "")),
                "dcterms_access_rights": blank(access),
                "dcterms_provenance": blank(raw.get("dcterms_provenance", "")),
                "dcterms_spatial": blank(raw.get("dcterms_spatial", "")),
                "dcterms_temporal": blank(raw.get("dcterms_temporal", "")),
                "dc_date": dc_date,
                "date_text": date_text,
                "bccns_access_status": "online" if "publicly viewable" in access.lower() else None,
                "bccns_digitized": bool(raw.get("media_url", "").strip()),
                "people": split_pipe(raw.get("dc_creator", "")),
                "subjects": split_pipe(raw.get("dc_subject", "")),
                "places": split_pipe(raw.get("dcterms_spatial", "")),
                "raw_metadata": {key: raw.get(key, "") for key in raw},
            }
        )
    if len(used) != len(prepared):
        raise SystemExit("dc_identifier values are not unique after the slug rule.")
    counts: dict[str, int] = {}
    for row in prepared:
        publisher = str(row["dc_publisher"])
        counts[publisher] = counts.get(publisher, 0) + 1
    if counts != EXPECTED_COUNTS:
        raise SystemExit(f"CSV publisher counts are {counts}, expected {EXPECTED_COUNTS}.")
    return prepared


def unique_identifier(identifier: str, used: set[str]) -> str:
    if identifier not in used:
        return identifier
    number = 2
    while f"{identifier}-{number}" in used:
        number += 1
    return f"{identifier}-{number}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "record"


def split_pipe(value: str) -> list[str]:
    found: list[str] = []
    for part in value.split("|"):
        name = part.strip()
        if name and name not in found:
            found.append(name)
    return found


def blank(value: str) -> str | None:
    value = value.strip()
    return value or None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_csv(argument: str | None) -> Path:
    if argument:
        path = Path(argument).expanduser()
        if not path.is_file():
            raise SystemExit(f"CSV not found: {path}")
        return path
    candidates = [
        ROOT / "data" / "black_history_metadata.csv",
        ROOT / "output" / "black_history_metadata.csv",
        ROOT.parent / "RBC HackHub" / "data" / "black_history_metadata.csv",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("CSV not found. Pass the path to black_history_metadata.csv.")


def load_env() -> None:
    for path in (ROOT / "scraper" / ".env", ROOT / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


def assert_service_role(key: str) -> None:
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except (IndexError, json.JSONDecodeError, ValueError):
        role = None
    if role != "service_role":
        raise SystemExit("SUPABASE_SECRET_KEY must be the service-role key, not the publishable key.")


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.client = httpx.Client(timeout=120)
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def upsert_names(self, table: str, names: set[str]) -> dict[str, str]:
        rows = [{"name": name} for name in sorted(names)]
        saved = self.upsert(table, rows, "name", "id,name")
        return {row["name"]: row["id"] for row in saved}

    def upsert(self, table: str, rows: list[dict[str, object]], on_conflict: str, select: str) -> list[dict]:
        saved: list[dict] = []
        for batch in chunks(rows, 100 if table == "records" else 400):
            response = self.request(
                "POST",
                table,
                params={"on_conflict": on_conflict, "select": select},
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                json=batch,
            )
            saved.extend(response.json())
        return saved

    def remove_stale_links(
        self,
        people: dict[str, set[str]],
        subjects: dict[str, set[str]],
        places: dict[str, set[str]],
    ) -> int:
        removed = 0
        removed += self._remove_pairs(
            "record_people",
            "person_id",
            people,
            self.fetch_all("record_people", "record_id,person_id,role"),
            creator_only=True,
        )
        removed += self._remove_pairs(
            "record_subjects",
            "subject_id",
            subjects,
            self.fetch_all("record_subjects", "record_id,subject_id"),
            creator_only=False,
        )
        removed += self._remove_pairs(
            "record_places",
            "place_id",
            places,
            self.fetch_all("record_places", "record_id,place_id"),
            creator_only=False,
        )
        return removed

    def _remove_pairs(
        self,
        table: str,
        other_key: str,
        desired: dict[str, set[str]],
        existing: list[dict[str, str]],
        *,
        creator_only: bool,
    ) -> int:
        stale = []
        for row in existing:
            record_id = row["record_id"]
            if record_id not in desired:
                continue
            if creator_only and row.get("role") != "creator":
                continue
            if row[other_key] not in desired[record_id]:
                stale.append((record_id, row[other_key]))
        for batch in chunks(stale, 20):
            parts = [f"and(record_id.eq.{record_id},{other_key}.eq.{other_id})" for record_id, other_id in batch]
            self.request("DELETE", table, params={"or": f"({','.join(parts)})"})
        return len(stale)

    def remove_unlinked_combined_names(self) -> int:
        removed = 0
        for person in self.fetch_all("people", "id,name"):
            if "|" not in person["name"]:
                continue
            links = self.request(
                "GET",
                "record_people",
                params={"select": "record_id", "person_id": f"eq.{person['id']}", "limit": "1"},
            ).json()
            if links:
                continue
            self.request("DELETE", "people", params={"id": f"eq.{person['id']}"})
            removed += 1
        return removed

    def publisher_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.fetch_all("records", "dc_publisher"):
            name = row["dc_publisher"]
            counts[name] = counts.get(name, 0) + 1
        return counts

    def fetch_all(self, table: str, select: str) -> list[dict]:
        rows: list[dict] = []
        start = 0
        page = 1000
        while True:
            response = self.request(
                "GET",
                table,
                params={"select": select},
                headers={"Range": f"{start}-{start + page - 1}"},
            )
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page:
                return rows
            start += page

    def request(self, method: str, table: str, *, params=None, headers=None, json=None) -> httpx.Response:
        last = ""
        for attempt in range(4):
            response = self.client.request(
                method,
                f"{self.base}/{table}",
                params=params,
                headers={**self.headers, **(headers or {})},
                json=json,
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                last = response.text
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise SystemExit(f"{method} {table} failed ({response.status_code}): {response.text}")
            return response
        raise SystemExit(f"{method} {table} failed after retries: {last}")


def chunks(rows: list, size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


if __name__ == "__main__":
    raise SystemExit(main())
