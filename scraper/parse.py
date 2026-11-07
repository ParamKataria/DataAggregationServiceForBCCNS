"""Turn one Nova Scotia Archives item page into a Dublin Core row."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.schema import MULTI_VALUE
from scraper.sources import BASE_URL, Source

# Longer names first so "Upper Hammonds Plains" wins over "Hammonds Plains".
PLACES = (
    "Upper Hammonds Plains",
    "Hammonds Plains",
    "Five Mile Plains",
    "Bedford Basin",
    "Cherrybrook",
    "Lucasville",
    "New Glasgow",
    "Cape Breton",
    "Sierra Leone",
    "Africville",
    "Birchtown",
    "Beechville",
    "Sunnyville",
    "Guysborough",
    "Shelburne",
    "Annapolis",
    "Dartmouth",
    "Liverpool",
    "Yarmouth",
    "Preston",
    "Halifax",
    "Truro",
    "Sydney",
    "Windsor",
    "Digby",
    "New York",
    "Trinidad",
    "Jamaica",
    "Prince Edward Island",
    "British Columbia",
    "Newfoundland",
    "New Brunswick",
    "Saskatchewan",
    "Upper Canada",
    "Lower Canada",
    "Port Roseway",
    "Freetown",
    "Hamilton",
    "Toronto",
    "Montreal",
    "Ottawa",
    "Vancouver",
    "Chatham",
    "Buxton",
    "Elgin",
    "Ontario",
    "Quebec",
    "Manitoba",
    "Alberta",
    "Nova Scotia",
)

CREATOR_LABELS = {
    "photographer",
    "engraver and publisher",
    "engraver",
    "publisher",
    "artist",
    "author",
    "creator",
    "painter",
    "cartographer",
    "compiler",
    "writer",
    "sculptor",
}

ACCESS_RIGHTS = (
    "Publicly viewable on the Nova Scotia Archives website. "
    "Reproduction or publication may require permission from the holding institution."
)


def parse_item(html: str, url: str, source: Source) -> dict[str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    if title_tag and "error" in title_tag.get_text(" ", strip=True).lower():
        return None
    if "cannot be found" in html.lower() and soup.find("h5") is None:
        return None

    heading = soup.find("h5")
    if heading is None:
        return None
    title = _strip_wrapping_quotes(_clean(heading.get_text(" ", strip=True)))
    if not title or title.lower().startswith("results "):
        return None

    fields, description = _body_fields(heading)
    date_text = fields.get("date", "")
    reference = _clean_reference(fields.get("reference", ""))
    format_text = fields.get("format", "")
    creators = [fields[label] for label in CREATOR_LABELS if fields.get(label)]

    created, issued = _split_date(date_text)
    spatial = find_places(f"{title} {description} {reference} {source.collection}")
    temporal = issued or created
    coverage = "; ".join(part for part in (spatial, temporal) if part)
    resource_type = _resource_type(title, format_text, reference)
    media_url = _media_url(soup)
    if format_text:
        archival_format = format_text
    elif media_url and resource_type == "Document":
        archival_format = "Digital image of an archival document"
    elif media_url:
        archival_format = "image/jpeg"
    else:
        archival_format = "text/html"

    publisher = _meta(soup, "author") or _meta(soup, "dc.creator") or "Nova Scotia Archives"
    rights = _meta(soup, "dc.rights")
    language = _meta(soup, "dc.language") or "eng"
    subjects = list(source.subjects)

    provenance_parts = [f"Held by {publisher}."]
    if reference:
        provenance_parts.append(f"Archival citation: {reference}")
    provenance_parts.append(f"Described from the virtual exhibit {source.collection}.")

    return {
        "dc_title": title,
        "dc_creator": MULTI_VALUE.join(creators),
        "dc_description": description,
        "dc_subject": MULTI_VALUE.join(subjects),
        "dc_date": date_text,
        "dcterms_created": created,
        "dcterms_issued": issued,
        "dc_type": resource_type,
        "dc_publisher": publisher,
        "dc_identifier": url,
        "dc_source": reference,
        "dc_coverage": coverage,
        "dcterms_spatial": spatial,
        "dcterms_temporal": temporal,
        "dc_relation": source.relation_url,
        "dc_language": language,
        "dc_format": archival_format,
        "dc_rights": rights,
        "dcterms_accessRights": ACCESS_RIGHTS,
        "dcterms_provenance": " ".join(provenance_parts),
        "collection": source.collection,
        "media_url": media_url,
    }


def parse_result_page(html: str) -> tuple[list[int], int | None, int | None]:
    """Return item ids, the last index shown, and the reported total."""
    ids: list[int] = []
    seen: set[int] = set()
    for match in re.finditer(r"archives/\?ID=(\d+)", html):
        item_id = int(match.group(1))
        if item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)

    count = re.search(r"Results\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)", html, re.I)
    if not count:
        return ids, None, None
    return ids, int(count.group(2)), int(count.group(3))


def _body_fields(heading) -> tuple[dict[str, str], str]:
    fields: dict[str, str] = {}
    description_parts: list[str] = []
    for paragraph in heading.find_all_next("p"):
        if paragraph.find_parent("footer") is not None:
            break
        if paragraph.find_parent(class_="bg-body-secondary") is not None:
            break
        classes = paragraph.get("class") or []
        if "d-print-block" in classes:
            break
        label, value = _paragraph_parts(paragraph)
        if label:
            fields.setdefault(label, value)
            continue
        if len(value) >= 40:
            description_parts.append(value)
    return fields, " ".join(description_parts)


def _paragraph_parts(paragraph) -> tuple[str | None, str]:
    text = _clean(paragraph.get_text(" ", strip=True))
    bold = paragraph.find("b")
    if bold is None:
        return None, text
    label = _clean(bold.get_text(" ", strip=True)).rstrip(":").lower()
    if not label or not text.lower().startswith(label):
        return None, text
    value = text[len(label) :].lstrip(" :").strip()
    return label, value


def _split_date(date_text: str) -> tuple[str, str]:
    """Return (created, issued). 'published' dates are issuance dates."""
    if not date_text:
        return "", ""
    if date_text.lower().startswith("published"):
        issued = date_text.split(None, 1)[1] if " " in date_text else date_text
        return "", issued.strip()
    return date_text, ""


def _resource_type(title: str, format_text: str, reference: str) -> str:
    # "negative" counts only in the format statement. Reference lines often say
    # "negative number" for a scan of a document.
    if re.search(r"\b(slide|transparency|photograph|photographs|negative)\b", format_text, re.I):
        return "Photograph"
    blob = f"{title} {reference}"
    if re.search(r"\b(map|plan)\b", blob, re.I):
        return "Map"
    if re.search(r"\b(newspaper|clarion|gazette|advocate)\b", blob, re.I):
        return "Newspaper"
    if re.search(r"\b(oral history|interview|sound recording)\b", blob, re.I):
        return "Oral history"
    if re.search(r"\bphotograph\b", title, re.I):
        return "Photograph"
    if re.search(r"\bportrait\b", title, re.I) and not re.search(r"\bpen portrait\b", title, re.I):
        return "Photograph"
    return "Document"


def find_places(text: str) -> str:
    found: list[str] = []
    for place in PLACES:
        if re.search(rf"\b{re.escape(place)}\b", text, re.I) and place not in found:
            found.append(place)
    return MULTI_VALUE.join(found)


def _media_url(soup: BeautifulSoup) -> str:
    image = soup.select_one("#printimage img") or soup.select_one("noscript img")
    if image and image.get("src"):
        return urljoin(BASE_URL, image["src"])
    og = soup.find("meta", property="og:image")
    if og and og.get("content") and "/images/" in og["content"]:
        return og["content"]
    return ""


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    if tag and tag.get("content"):
        return _clean(tag["content"])
    return ""


def _clean_reference(value: str) -> str:
    value = re.split(r"Request to buy", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"document\.write\([^)]*\)", "", value)
    return _clean(value).strip(" ;")


def _clean(value: str) -> str:
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _strip_wrapping_quotes(value: str) -> str:
    pairs = (("''", "''"), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))
    for start, end in pairs:
        if value.startswith(start) and value.endswith(end) and len(value) > len(start) + len(end):
            return value[len(start) : -len(end)].strip()
    return value
