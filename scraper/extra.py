"""Harvest the encyclopedia, BPHC, Black Loyalist, and LAC guide pages."""

from __future__ import annotations

import asyncio
import re
import sys
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from scraper.parse import find_places
from scraper.schema import COLUMNS, MULTI_VALUE

TCE_COLLECTION = "https://thecanadianencyclopedia.ca/en/collection/black-history-in-canada"
BPHC_FEED = "https://www.blackpeopleshistory.ca/feed/?paged={page}"
BPHC_HOME = "https://www.blackpeopleshistory.ca/"
CDC_START = "https://blackloyalist.com/cdc/wireframe.htm"
LAC_URL = (
    "https://www.canada.ca/en/library-archives/collection/research-help/"
    "genealogy-family-history/ethno-cultural/black-canadians.html"
)

MUSEUM_PAGES = (
    "https://blackloyalist.com/loyalist-history/",
    "https://blackloyalist.com/archaeology-history/",
    "https://blackloyalist.com/back-to-birchtown/",
)

ACCESS = "Publicly viewable online at the link in dc_identifier."
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
SKIP_CDC = ("index.htm", "wireframe.htm", "feedback.htm")


async def harvest_extra(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    limit_per_collection: int,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for label, coro in (
        ("Black History in Canada", harvest_encyclopedia(client, semaphore, limit_per_collection)),
        ("A Black People's History of Canada", harvest_bphc(client, semaphore, limit_per_collection)),
        ("Black Loyalists: Our Story, Our People", harvest_cdc(client, semaphore, limit_per_collection)),
        ("Black Loyalist Heritage Centre", harvest_museum(client, semaphore, limit_per_collection)),
        ("Library and Archives Canada", harvest_lac(client, semaphore, limit_per_collection)),
    ):
        found = await coro
        print(f"{label}: {len(found)} records", file=sys.stderr)
        records.extend(found)
    return records


async def harvest_encyclopedia(client, semaphore, limit: int) -> list[dict[str, str]]:
    html = await _get(client, semaphore, TCE_COLLECTION)
    urls = _collection_urls(html)
    if limit > 0:
        urls = urls[:limit]
    pages = await asyncio.gather(
        *[_get(client, semaphore, url, max_bytes=80_000) for url in urls]
    )
    records = []
    for url, page in zip(urls, pages, strict=True):
        if not page:
            continue
        record = _encyclopedia_record(url, page)
        if record:
            records.append(record)
    return records


async def harvest_bphc(client, semaphore, limit: int) -> list[dict[str, str]]:
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for page in range(1, 12):
        xml = await _get(client, semaphore, BPHC_FEED.format(page=page), quiet_status={404})
        if not xml:
            break
        fresh = []
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            link = _xml_text(item, "link")
            if not link or link in seen:
                continue
            seen.add(link)
            fresh.append(
                {
                    "link": link,
                    "title": _xml_text(item, "title"),
                    "creator": _xml_text(item, "dc:creator"),
                    "date": _iso_date(_xml_text(item, "pubDate")),
                    "description": _plain(_xml_text(item, "description")),
                    "categories": tuple(_xml_all(item, "category")),
                }
            )
        if not fresh:
            break
        items.extend(fresh)
    if limit > 0:
        items = items[:limit]

    pages = await asyncio.gather(*[_get(client, semaphore, item["link"]) for item in items])
    records = []
    for item, page in zip(items, pages, strict=True):
        creators = _contributors(page) if page else []
        if not creators and item["creator"]:
            creators = [item["creator"]]
        records.append(
            _row(
                title=item["title"],
                creators=creators,
                description=item["description"],
                subjects=("Black history in Canada", "African Canadians", *item["categories"]),
                issued=item["date"],
                resource_type=_bphc_type(item["categories"]),
                publisher="A Black People's History of Canada",
                url=item["link"],
                source="A Black People's History of Canada",
                relation=BPHC_HOME,
                collection="A Black People's History of Canada",
                rights="Published by A Black People's History of Canada.",
                media=_featured_image(page or ""),
            )
        )
    return records


async def harvest_cdc(client, semaphore, limit: int) -> list[dict[str, str]]:
    queue = [CDC_START]
    seen: set[str] = set()
    records: list[dict[str, str]] = []
    while queue and len(seen) < 500:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        html = await _get(client, semaphore, url)
        if not html:
            continue
        record = _cdc_record(url, html)
        if record:
            records.append(record)
        if limit > 0 and len(records) >= limit:
            break
        for link in _cdc_links(url, html):
            if link not in seen:
                queue.append(link)
    return records


async def harvest_museum(client, semaphore, limit: int) -> list[dict[str, str]]:
    urls = MUSEUM_PAGES if limit <= 0 else MUSEUM_PAGES[:limit]
    pages = await asyncio.gather(*[_get(client, semaphore, url) for url in urls])
    records = []
    for url, html in zip(urls, pages, strict=True):
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        title = _heading(soup) or _page_title(html)
        body = soup.select_one(".entry-content")
        description = _plain(body.get_text(" ", strip=True) if body else "")[:900]
        if not title or len(description) < 80:
            continue
        records.append(
            _row(
                title=title,
                creators=[],
                description=description,
                subjects=("Black Loyalists", "Birchtown", "Nova Scotia", "African Nova Scotians"),
                issued="",
                resource_type="Article",
                publisher="Black Loyalist Heritage Centre",
                url=url,
                source="Black Loyalist Heritage Centre",
                relation="https://blackloyalist.com/",
                collection="Black Loyalist Heritage Centre",
                rights="Published by the Black Loyalist Heritage Centre, a part of the Nova Scotia Museum.",
                media="",
            )
        )
    return records


async def harvest_lac(client, semaphore, limit: int) -> list[dict[str, str]]:
    html = ""
    for _attempt in range(4):
        try:
            html = await asyncio.to_thread(_sync_get, LAC_URL, 15.0)
        except Exception as exc:
            print(f"skip {LAC_URL}: {type(exc).__name__}", file=sys.stderr)
            html = ""
        if html and "Black Canadians" in html:
            break
        await asyncio.sleep(1.0)
    if not html or "Black Canadians" not in html:
        print("skip Library and Archives Canada: the guide page did not respond", file=sys.stderr)
        return []
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main") or soup
    heading = main.find("h1")
    title = heading.get_text(" ", strip=True) if heading else "Black Canadians"
    intro_parts: list[str] = []
    if heading is not None:
        for element in heading.find_all_next():
            if element.name == "h2":
                break
            if element.name == "p":
                intro_parts.append(element.get_text(" ", strip=True))
    records = [
        _lac_row(title, " ".join(intro_parts)[:700]),
    ]
    for section in main.find_all("h3"):
        name = section.get_text(" ", strip=True)
        if name.lower() in {"page details"}:
            continue
        details = section.find_parent("details")
        if details is not None:
            description = details.get_text(" ", strip=True).replace(name, "", 1)
        else:
            bits: list[str] = []
            for sibling in section.find_next_siblings():
                if getattr(sibling, "name", None) in {"h2", "h3"}:
                    break
                text = sibling.get_text(" ", strip=True)
                if text:
                    bits.append(text)
            description = " ".join(bits)
        description = _plain(re.sub(r"\b(Expand all|Collapse all)\b", "", description))[:1200]
        if len(description) < 60:
            continue
        anchor = section.get("id") or ""
        if not anchor:
            parent = section.find_parent(id=True)
            anchor = parent.get("id", "") if parent else ""
        if not anchor:
            anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        records.append(_lac_row(name, description, f"{LAC_URL}#{anchor}"))
        if limit > 0 and len(records) >= limit:
            break
    return records


def _lac_row(title: str, description: str, url: str = LAC_URL) -> dict[str, str]:
    return _row(
        title=title,
        creators=[],
        description=description,
        subjects=("Black Canadians", "Genealogy", "Family history"),
        issued="",
        resource_type="Research guide",
        publisher="Library and Archives Canada",
        url=url,
        source="Library and Archives Canada",
        relation=LAC_URL,
        collection="Library and Archives Canada: Black Canadians",
        rights="Published by Library and Archives Canada.",
        media="",
    )


def _collection_urls(html: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for url in re.findall(r'href="(https://thecanadianencyclopedia.ca/en/article/[^"]+)"', html):
        if url not in seen:
            seen.add(url)
            found.append(url)
    for url in re.findall(r'href="(https://thecanadianencyclopedia.ca/en/(?:studyguide|timeline)/[^"]+)"', html):
        slug = url.rstrip("/").split("/")[-1]
        if "black" not in slug and "negroes" not in slug:
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _encyclopedia_record(url: str, html: str) -> dict[str, str] | None:
    title = _meta(html, "og:title") or _page_title(html)
    description = _json_description(html) or _plain(
        _meta(html, "og:description") or _meta(html, "description")
    )
    if not title or title.lower().startswith("the canadian encyclopedia"):
        return None
    author_block = re.search(r'"author"\s*:\s*\[(.*?)\]', html, re.S)
    creators = re.findall(r'"name"\s*:\s*"([^"]+)"', author_block.group(1)) if author_block else []
    published = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    issued = published.group(1) if published else ""
    if "/studyguide/" in url:
        resource_type = "Educational resource"
    elif "/timeline/" in url:
        resource_type = "Timeline"
    elif re.search(r"\bborn\b", description, re.I):
        resource_type = "Biography"
    else:
        resource_type = "Article"
    return _row(
        title=title,
        creators=creators,
        description=description,
        subjects=("Black history in Canada", "African Canadians"),
        issued=issued,
        resource_type=resource_type,
        publisher="The Canadian Encyclopedia",
        url=url,
        source="The Canadian Encyclopedia",
        relation=TCE_COLLECTION,
        collection="Black History in Canada",
        rights="Published by The Canadian Encyclopedia.",
        media=_meta(html, "og:image"),
    )


def _cdc_links(url: str, html: str) -> list[str]:
    links = []
    for href in re.findall(r'href="([^"]+)"', html, re.I):
        absolute = urljoin(url, href.split("#")[0])
        if "blackloyalist.com/cdc/" not in absolute:
            continue
        if not absolute.lower().endswith((".htm", ".html")):
            continue
        links.append(absolute)
    return links


def _cdc_record(url: str, html: str) -> dict[str, str] | None:
    lower = url.lower()
    if lower.endswith(SKIP_CDC):
        return None
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = [_plain(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) >= 80]
    if not paragraphs:
        return None
    title = _page_title(html) or paragraphs[0][:80]
    description = " ".join(paragraphs[:3])[:900]
    if "/people/" in lower:
        resource_type = "Biography"
    elif "/documents/" in lower:
        resource_type = "Document"
    else:
        resource_type = "Article"
    return _row(
        title=title,
        creators=[],
        description=description,
        subjects=("Black Loyalists", "Birchtown", "Nova Scotia", "African Nova Scotians"),
        issued="",
        resource_type=resource_type,
        publisher="Black Loyalist Heritage Centre",
        url=url,
        source="Canada's Digital Collections: Black Loyalists: Our Story, Our People",
        relation="https://blackloyalist.com/cdc/index.htm",
        collection="Black Loyalists: Our Story, Our People",
        rights="Published in Canada's Digital Collections and hosted by the Black Loyalist Heritage Centre.",
        media="",
    )


def _row(
    *,
    title: str,
    creators: list[str],
    description: str,
    subjects: tuple[str, ...] | list[str],
    issued: str,
    resource_type: str,
    publisher: str,
    url: str,
    source: str,
    relation: str,
    collection: str,
    rights: str,
    media: str,
) -> dict[str, str]:
    temporal = _coverage_years(description)
    spatial = find_places(f"{title} {description} {collection}")
    unique_subjects: list[str] = []
    for subject in subjects:
        cleaned = _plain(subject)
        if cleaned and cleaned not in unique_subjects and cleaned.lower() not in {"links", "resources"}:
            unique_subjects.append(cleaned)
    provenance = f"Held online by {publisher}. {source}."
    row = {column: "" for column in COLUMNS}
    row.update(
        {
            "dc_title": title,
            "dc_creator": MULTI_VALUE.join(creators),
            "dc_description": description,
            "dc_subject": MULTI_VALUE.join(unique_subjects),
            "dc_date": issued,
            "dcterms_created": "",
            "dcterms_issued": issued,
            "dc_type": resource_type,
            "dc_publisher": publisher,
            "dc_identifier": url,
            "dc_source": source,
            "dc_coverage": "; ".join(part for part in (spatial, temporal) if part),
            "dcterms_spatial": spatial,
            "dcterms_temporal": temporal,
            "dc_relation": relation,
            "dc_language": "eng",
            "dc_format": "text/html",
            "dc_rights": rights,
            "dcterms_accessRights": ACCESS,
            "dcterms_provenance": provenance,
            "collection": collection,
            "media_url": media,
        }
    )
    return row


def _contributors(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all(["h2", "h3"]):
        if "contributor" not in heading.get_text(" ", strip=True).lower():
            continue
        listing = heading.find_next("ul")
        if listing is None:
            return []
        return [item.get_text(" ", strip=True) for item in listing.find_all("li") if item.get_text(strip=True)]
    return []


def _featured_image(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    image = soup.select_one("img.wp-post-image") or soup.find("meta", property="og:image")
    if image is None:
        return ""
    return image.get("content") or image.get("src") or ""


def _bphc_type(categories: tuple[str, ...]) -> str:
    blob = " ".join(categories).lower()
    if "poster" in blob:
        return "Poster"
    if "webinar" in blob:
        return "Webinar"
    if "magazine" in blob:
        return "Magazine"
    return "Educational resource"


def _coverage_years(text: str) -> str:
    born = re.search(r"\bborn\b[^.]{0,50}?\b(1[6-9]\d{2}|20\d{2})\b", text, re.I)
    died = re.search(r"\bdied\b[^.]{0,50}?\b(1[6-9]\d{2}|20\d{2})\b", text, re.I)
    if born and died:
        return f"{born.group(1)}-{died.group(1)}"
    span = re.search(
        r"\b(1[6-9]\d{2}|20\d{2})\s*(?:–|-|—|to)\s*(1[6-9]\d{2}|20\d{2})\b",
        text,
    )
    if span:
        return f"{span.group(1)}-{span.group(2)}"
    return ""


def _sync_get(url: str, timeout: float) -> str:
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as sync_client:
        response = sync_client.get(url)
        response.raise_for_status()
        return response.text


async def _get(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int | None = None,
    quiet_status: set[int] | None = None,
) -> str:
    async with semaphore:
        try:
            text = await _read(client, url, timeout, max_bytes)
            await asyncio.sleep(0.08)
            return text
        except httpx.HTTPStatusError as exc:
            if quiet_status and exc.response.status_code in quiet_status:
                await asyncio.sleep(0.08)
                return ""
            print(f"skip {url}: {exc}", file=sys.stderr)
            await asyncio.sleep(0.08)
            return ""
        except Exception as exc:
            print(f"skip {url}: {exc}", file=sys.stderr)
            await asyncio.sleep(0.08)
            return ""


async def _read(client, url: str, timeout: float, max_bytes: int | None) -> str:
    if max_bytes is None:
        response = await client.get(url, timeout=timeout, headers=BROWSER_HEADERS)
        response.raise_for_status()
        return response.text
    async with client.stream("GET", url, timeout=timeout, headers=BROWSER_HEADERS) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) >= max_bytes:
                break
        return bytes(data).decode("utf-8", "replace")


def _json_description(html: str) -> str:
    match = re.search(
        r'"description"\s*:\s*"(.*?)"\s*,\s*"(?:author|revisor|publisher|image|datePublished)"',
        html,
        re.S,
    )
    if not match:
        return ""
    text = match.group(1).replace("\\n", " ").replace('\\"', '"')
    return _plain(text)


def _meta(html: str, key: str) -> str:
    pattern = rf'<meta[^>]+(?:property|name)="{re.escape(key)}"[^>]+content="([^"]*)"'
    match = re.search(pattern, html, re.I)
    if not match:
        pattern = rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(key)}"'
        match = re.search(pattern, html, re.I)
    return unescape(match.group(1)) if match else ""


def _page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not match:
        return ""
    title = _plain(match.group(1))
    title = re.sub(r"\s*[|–-]\s*(The Canadian Encyclopedia|BPHC|Black Loyalist Heritage Centre).*$", "", title)
    return title.strip(" -|")


def _heading(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    return heading.get_text(" ", strip=True) if heading else ""


def _xml_text(item: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
    return _plain(match.group(1)) if match else ""


def _xml_all(item: str, tag: str) -> list[str]:
    return [
        _plain(value)
        for value in re.findall(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
        if _plain(value)
    ]


def _iso_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return ""


def _plain(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()
