"""Import any public page into the existing Dublin Core columns.

Metadata is taken from the page's own Dublin Core first. schema.org, citation
tags, and ordinary HTML metadata fill only the gaps. The article body is not
copied. Every listed resource is still recorded, with the publisher, rights,
source, and URL kept so it can be credited.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from scraper.extra import _coverage_years, _plain
from scraper.harvest import HarvestError
from scraper.parse import find_places
from scraper.schema import COLUMNS, MULTI_VALUE
from scraper.sources import USER_AGENT

FETCH_CAP = 400
PAGE_CAP = 40
_DESCRIPTION_LIMIT = 1200

_MULTI = {"dc_creator", "dc_subject", "dc_publisher"}
_SKIP_TYPES = {
    "website",
    "organization",
    "person",
    "breadcrumblist",
    "searchaction",
    "entrypoint",
    "imageobject",
    "siteNavigationelement",
    "wpheader",
    "wpfooter",
    "propertyvalue",
    "postaladdress",
    "place",
    "action",
    "viewaction",
    "readaction",
}
_WORK_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "scholarlyarticle",
    "report",
    "digitaldocument",
    "manuscript",
    "chapter",
    "book",
    "thesis",
    "photograph",
    "painting",
    "visualartwork",
    "map",
    "newspaper",
    "periodical",
    "publicationissue",
    "videoobject",
    "clip",
    "audioobject",
    "podcastseries",
    "podcastepisode",
    "creativework",
    "webpage",
    "collection",
    "collectionpage",
    "course",
    "learningresource",
    "dataset",
    "poster",
    "presentationdigitaldocument",
    "listitem",
}
_TYPE_MAP = {
    "article": "Article",
    "newsarticle": "Article",
    "blogposting": "Article",
    "scholarlyarticle": "Article",
    "report": "Document",
    "digitaldocument": "Document",
    "manuscript": "Document",
    "chapter": "Document",
    "book": "Document",
    "thesis": "Document",
    "webpage": "Document",
    "photograph": "Photograph",
    "painting": "Photograph",
    "visualartwork": "Photograph",
    "imageobject": "Photograph",
    "map": "Map",
    "newspaper": "Newspaper",
    "periodical": "Magazine",
    "publicationissue": "Magazine",
    "videoobject": "Video",
    "clip": "Video",
    "audioobject": "Oral history",
    "podcastseries": "Oral history",
    "podcastepisode": "Oral history",
    "person": "Biography",
    "collection": "Collection",
    "collectionpage": "Collection",
    "course": "Educational resource",
    "learningresource": "Educational resource",
    "poster": "Poster",
}
_DC_FIELDS = {
    "dc.title": "dc_title",
    "dcterms.title": "dc_title",
    "dc.creator": "dc_creator",
    "dcterms.creator": "dc_creator",
    "dc.subject": "dc_subject",
    "dcterms.subject": "dc_subject",
    "dc.description": "dc_description",
    "dcterms.description": "dc_description",
    "dc.publisher": "dc_publisher",
    "dcterms.publisher": "dc_publisher",
    "dc.date": "dc_date",
    "dcterms.date": "dc_date",
    "dcterms.created": "dcterms_created",
    "dcterms.issued": "dcterms_issued",
    "dc.type": "dc_type",
    "dcterms.type": "dc_type",
    "dc.format": "dc_format",
    "dcterms.format": "dc_format",
    "dc.identifier": "dc_identifier",
    "dcterms.identifier": "dc_identifier",
    "dc.source": "dc_source",
    "dcterms.source": "dc_source",
    "dc.language": "dc_language",
    "dcterms.language": "dc_language",
    "dc.relation": "dc_relation",
    "dcterms.relation": "dc_relation",
    "dc.coverage": "dc_coverage",
    "dcterms.coverage": "dc_coverage",
    "dcterms.spatial": "dcterms_spatial",
    "dc.coverage.spatial": "dcterms_spatial",
    "dcterms.temporal": "dcterms_temporal",
    "dc.coverage.temporal": "dcterms_temporal",
    "dc.rights": "dc_rights",
    "dcterms.rights": "dc_rights",
    "dcterms.accessrights": "dcterms_accessRights",
    "dcterms.provenance": "dcterms_provenance",
}
_LANG = {
    "en": "eng",
    "fr": "fre",
    "de": "ger",
    "es": "spa",
    "pt": "por",
    "nl": "dut",
    "it": "ita",
    "zh": "chi",
    "ja": "jpn",
    "ko": "kor",
    "ar": "ara",
}
_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".xml": "application/xml",
}
_SKIP_PATH = re.compile(
    r"/(login|signin|sign-in|signup|sign-up|register|cart|checkout|account|"
    r"search|wp-admin|wp-login|feed|xmlrpc|privacy|terms|cookie-policy|cookies)(/|$)",
    re.I,
)
_SHARE = re.compile(r"(sharer\.php|intent/tweet|/share\?|sharing/share)", re.I)
_CARD = re.compile(r"\b(card|result|entry|item|record|resource)\b", re.I)
_BIB = re.compile(r"\b(references|bibliography|works cited|further reading|sources)\b", re.I)
_GENERIC_TITLES = {"read more", "click here", "here", "more", "pdf", "download", "link", "source"}
_JSON_KEYS = ("@graph", "mainEntity", "hasPart", "itemListElement", "item", "citation", "isBasedOn")


@dataclass(frozen=True)
class Link:
    url: str
    title: str
    found_on: str
    fetch: bool
    mime: str = ""


def prepare_url(value: str) -> str:
    """Accept a full URL, or a host name with https added."""
    value = value.strip()
    if any(character.isspace() for character in value):
        raise ValueError(f"Not a page URL: {value}")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parts = urlsplit(value)
    host = parts.hostname or ""
    if not host or ("." not in host and host != "localhost"):
        raise ValueError(f"Not a page URL: {value}")
    return value


async def harvest_url(start_url: str, limit: int = 0) -> list[dict[str, str]]:
    """Read one page and every resource it lists. Rows use the usual columns."""
    start_url = prepare_url(start_url)
    retrieved_on = date.today().isoformat()
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    timeout = httpx.Timeout(30.0)
    limits = httpx.Limits(max_connections=6, max_keepalive_connections=6)
    semaphore = asyncio.Semaphore(4)
    robots: dict[str, RobotFileParser] = {}

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        limits=limits,
    ) as client:

        async def allowed(url: str) -> bool:
            netloc = urlsplit(url).netloc.lower()
            if netloc not in robots:
                robots[netloc] = await _load_robots(client, url)
            return robots[netloc].can_fetch(USER_AGENT, url)

        async def fetch(url: str) -> tuple[str, str, str] | None:
            async with semaphore:
                try:
                    result = await _fetch_page(client, url)
                except Exception as exc:
                    print(f"skip {url}: {exc}", file=sys.stderr)
                    result = None
                await asyncio.sleep(0.12)
                return result

        if not await allowed(start_url):
            print(f"robots.txt disallows {start_url}", file=sys.stderr)
            host = _host(start_url)
            return [
                _credit_only(
                    url=start_url,
                    title=_title_from_url(start_url),
                    found_on=start_url,
                    retrieved_on=retrieved_on,
                    publisher=host,
                    collection=host,
                    mime="",
                    reason=f"Not retrieved because robots.txt disallows {start_url}.",
                )
            ]

        fetched = await fetch(start_url)
        if not fetched or not fetched[1].lstrip().startswith("<"):
            host = _host(start_url)
            mime = "" if not fetched else _mime_from_type(fetched[0])
            return [
                _credit_only(
                    url=start_url,
                    title=_title_from_url(start_url),
                    found_on=start_url,
                    retrieved_on=retrieved_on,
                    publisher=host,
                    collection=host,
                    mime=mime,
                    reason=f"The page at {start_url} did not return readable metadata.",
                )
            ]

        _ctype, seed_html, final_url = fetched
        home = final_url or start_url
        site = site_name_from(seed_html, home)
        records = [
            metadata_record(
                seed_html,
                home,
                found_on=home,
                collection=site,
                retrieved_on=retrieved_on,
            )
        ]
        links, next_pages = discover(seed_html, home)
        seen_pages = {_key(home)}
        pages = 0
        while next_pages and pages < PAGE_CAP:
            page_url = next_pages.pop(0)
            if not page_url or _key(page_url) in seen_pages or not _same_site(page_url, home):
                continue
            seen_pages.add(_key(page_url))
            if not await allowed(page_url):
                print(f"robots.txt disallows listing page {page_url}", file=sys.stderr)
                continue
            pages += 1
            result = await fetch(page_url)
            if not result or not result[1].lstrip().startswith("<"):
                continue
            more_links, more_next = discover(result[1], result[2] or page_url)
            links.extend(more_links)
            next_pages.extend(more_next)

        chosen: list[Link] = []
        seen_urls = {_key(home)}
        for link in links:
            key = _key(link.url)
            if not key or key in seen_urls or _skip_url(link.url):
                continue
            seen_urls.add(key)
            chosen.append(link)
        if limit > 0:
            chosen = chosen[: max(limit - 1, 0)]

        fetched_count = 0

        async def one(link: Link) -> dict[str, str]:
            nonlocal fetched_count
            publisher, collection = _party(link.url, site, home)
            if link.mime or not link.fetch or not _same_site(link.url, home):
                return _credit_only(
                    url=link.url,
                    title=link.title,
                    found_on=link.found_on,
                    retrieved_on=retrieved_on,
                    publisher=publisher,
                    collection=collection,
                    mime=link.mime,
                    reason=f"Linked from {link.found_on}.",
                )
            if fetched_count >= FETCH_CAP:
                return _credit_only(
                    url=link.url,
                    title=link.title,
                    found_on=link.found_on,
                    retrieved_on=retrieved_on,
                    publisher=publisher,
                    collection=collection,
                    mime="",
                    reason=(
                        f"Not retrieved. This import stops after {FETCH_CAP} pages "
                        f"so the rest of the site is not crawled. Listed on {link.found_on}."
                    ),
                )
            fetched_count += 1
            if not await allowed(link.url):
                return _credit_only(
                    url=link.url,
                    title=link.title,
                    found_on=link.found_on,
                    retrieved_on=retrieved_on,
                    publisher=publisher,
                    collection=collection,
                    mime="",
                    reason=(
                        f"Not retrieved because robots.txt disallows {link.url}. "
                        f"Listed on {link.found_on}."
                    ),
                )
            result = await fetch(link.url)
            if not result or not result[1].lstrip().startswith("<"):
                mime = link.mime or (_mime_from_type(result[0]) if result else "")
                return _credit_only(
                    url=link.url,
                    title=link.title,
                    found_on=link.found_on,
                    retrieved_on=retrieved_on,
                    publisher=publisher,
                    collection=collection,
                    mime=mime,
                    reason=f"Listed on {link.found_on}. The page did not return readable metadata.",
                )
            return metadata_record(
                result[1],
                result[2] or link.url,
                found_on=link.found_on,
                collection=site if _same_site(result[2] or link.url, home) else _host(link.url),
                retrieved_on=retrieved_on,
                fallback_title=link.title,
            )

        if chosen:
            records.extend(await asyncio.gather(*(one(link) for link in chosen)))

    credited = sum(record["dcterms_provenance"].startswith("Recorded on ") for record in records)
    described = len(records) - credited
    print(
        f"{_host(home)}: {len(records)} resources "
        f"({described} described from page metadata, {credited} credited from the link)",
        file=sys.stderr,
    )
    return records


def metadata_record(
    html: str,
    page_url: str,
    *,
    found_on: str,
    collection: str,
    retrieved_on: str,
    fallback_title: str = "",
) -> dict[str, str]:
    """Map one HTML page onto the CSV columns. Dublin Core values win."""
    soup = BeautifulSoup(html, "html.parser")
    meta = _meta_map(soup)
    nodes = _jsonld_nodes(soup)
    row = {column: "" for column in COLUMNS}
    contributors: list[str] = []
    _apply_schema(row, _primary(nodes, page_url), contributors)
    _apply_citation(row, meta)
    _apply_open_graph(row, meta)
    _apply_html(row, soup, meta)
    _apply_dc(row, meta, contributors)
    _apply_license(row, soup, page_url)
    _normalize_language(row)
    _backfill_dates(row)
    _backfill_coverage(row)
    if not row["dc_format"]:
        row["dc_format"] = "text/html"
    if not row["dc_type"]:
        row["dc_type"] = "Document"
    other_ids = _prefer_page_url(row, soup, page_url)
    _remember_other_ids(row, other_ids)
    publisher = row["dc_publisher"] or collection or _host(page_url)
    row["dc_publisher"] = publisher
    row["collection"] = collection or _host(page_url)
    if not row["dc_rights"]:
        row["dc_rights"] = f"Rights remain with {publisher}."
    if not row["dcterms_accessRights"]:
        row["dcterms_accessRights"] = (
            "Metadata only. The publisher named in dc_publisher decides access to the work."
        )
    if not row["dc_source"]:
        row["dc_source"] = f"{publisher} ({row['dc_identifier']})"
    if not row["dc_relation"]:
        if found_on and _key(found_on) != _key(row["dc_identifier"]):
            row["dc_relation"] = found_on
        else:
            row["dc_relation"] = _origin(page_url)
    if fallback_title and (not row["dc_title"] or row["dc_title"] == row["dc_identifier"]):
        row["dc_title"] = fallback_title
    if not row["dc_title"]:
        row["dc_title"] = row["dc_identifier"] or page_url
    note = (
        f"Metadata recorded on {retrieved_on} from {row['dc_identifier']}. "
        "Values come from the page's own Dublin Core, schema.org, or HTML metadata "
        "so the source can be credited. The work was not copied."
    )
    if found_on and _key(found_on) != _key(row["dc_identifier"]):
        note += f" Listed on {found_on}."
    names = _unique(contributors)
    if names:
        note += f" Contributors: {MULTI_VALUE.join(names)}."
    row["dcterms_provenance"] = _sentence(row["dcterms_provenance"], note)
    return row


def discover(html: str, page_url: str) -> tuple[list[Link], list[str]]:
    """Resources linked from this page, plus the next listing page when this is a list."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = _jsonld_nodes(soup)
    links = _jsonld_links(nodes, page_url)
    root = soup.select_one("main, article, [role=main]") or soup.body or soup
    for anchor in _content_anchors(root):
        url = _abs(page_url, anchor.get("href", ""))
        if not url or _skip_url(url):
            continue
        mime = _mime_for(url)
        if not mime and anchor.get("download") is None:
            continue
        links.append(Link(url, _link_title(anchor, url), page_url, False, mime or "application/octet-stream"))
    for anchor in _list_links(root) + _heading_links(root) + _card_links(root):
        url = _abs(page_url, anchor.get("href", ""))
        if not url or _skip_url(url):
            continue
        mime = _mime_for(url)
        links.append(
            Link(url, _link_title(anchor, url), page_url, _same_site(url, page_url) and not mime, mime)
        )
    for anchor in _bibliography_links(soup):
        url = _abs(page_url, anchor.get("href", ""))
        if not url or _skip_url(url):
            continue
        mime = _mime_for(url)
        links.append(
            Link(url, _link_title(anchor, url), page_url, _same_site(url, page_url) and not mime, mime)
        )
    next_pages = _next_urls(soup, page_url) if _is_listing(soup, nodes, root, page_url) else []
    return links, next_pages


def site_name_from(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    meta = _meta_map(soup)
    for key in ("og:site_name", "application-name", "dc.publisher", "dcterms.publisher"):
        if meta.get(key):
            return meta[key][0]
    for node in _jsonld_nodes(soup):
        types = _type_tokens(node)
        if "website" in types or "organization" in types:
            names = _text_list(node.get("name"))
            if names:
                return names[0]
        names = _text_list(node.get("publisher"))
        if names:
            return names[0]
    return _host(url)


def _credit_only(
    *,
    url: str,
    title: str,
    found_on: str,
    retrieved_on: str,
    publisher: str,
    collection: str,
    mime: str,
    reason: str,
) -> dict[str, str]:
    row = {column: "" for column in COLUMNS}
    row["dc_title"] = title or _title_from_url(url)
    row["dc_identifier"] = url
    row["dc_type"] = _type_for_mime(mime)
    row["dc_format"] = mime
    row["dc_publisher"] = publisher or _host(url)
    row["dc_source"] = f"{row['dc_publisher']} ({url})"
    row["dc_relation"] = _origin(url) if _key(found_on) == _key(url) else found_on
    row["collection"] = collection or row["dc_publisher"]
    row["dc_rights"] = f"Rights remain with {row['dc_publisher']}."
    row["dcterms_accessRights"] = (
        "Metadata only, recorded so this resource can be credited. "
        "The publisher decides access to the work."
    )
    row["dcterms_provenance"] = (
        f"Recorded on {retrieved_on} for attribution. {reason} The work was not copied."
    )
    row["dcterms_spatial"] = find_places(row["dc_title"])
    if row["dcterms_spatial"]:
        row["dc_coverage"] = row["dcterms_spatial"]
    return row


def _apply_schema(row: dict[str, str], node: dict | None, contributors: list[str]) -> None:
    if not node:
        return
    _set(row, "dc_title", _text_list(node.get("headline") or node.get("name")))
    _set(row, "dc_description", [_clip(value) for value in _text_list(node.get("description"))])
    _set(row, "dc_creator", _text_list(node.get("creator") or node.get("author")))
    contributors.extend(_text_list(node.get("contributor")))
    contributors.extend(_text_list(node.get("editor")))
    subjects = _text_list(node.get("keywords"))
    if len(subjects) == 1 and any(mark in subjects[0] for mark in (",", ";")):
        subjects = _split_keywords(subjects[0])
    subjects.extend(_text_list(node.get("about")))
    _set(row, "dc_subject", subjects)
    issued = _date_value(node.get("datePublished"))
    created = _date_value(node.get("dateCreated"))
    _set(row, "dcterms_issued", issued)
    _set(row, "dcterms_created", created)
    _set(row, "dc_type", _schema_type(node))
    _set(row, "dc_publisher", _text_list(node.get("publisher") or node.get("provider")))
    _set(row, "dc_language", _text_list(node.get("inLanguage")))
    spatial = _text_list(node.get("spatialCoverage") or node.get("contentLocation"))
    _set(row, "dcterms_spatial", spatial)
    _set(row, "dcterms_temporal", _text_list(node.get("temporalCoverage")))
    part = _first_http(node.get("isPartOf"))
    if part:
        _set(row, "dc_relation", part)
    rights = _text_list(node.get("copyrightNotice"))
    license_url = _first_http(node.get("license"))
    if license_url:
        rights.append(license_url)
    _set(row, "dc_rights", [" ".join(rights)] if rights else [])
    _set(row, "dcterms_accessRights", _text_list(node.get("conditionsOfAccess")))
    _set(row, "dc_format", _text_list(node.get("encodingFormat")))
    image = _image_url(_first_http(node.get("image") or node.get("thumbnailUrl")))
    if image:
        _set(row, "media_url", image)


def _apply_citation(row: dict[str, str], meta: dict[str, list[str]]) -> None:
    _set(row, "dc_title", meta.get("citation_title", []))
    _set(row, "dc_creator", meta.get("citation_author", []))
    _set(row, "dc_description", [_clip(value) for value in meta.get("citation_abstract", [])])
    _set(row, "dc_publisher", meta.get("citation_publisher", []))
    _set(row, "dcterms_issued", [_date_value(value) for value in meta.get("citation_publication_date", [])])
    _set(row, "dc_language", meta.get("citation_language", []))
    keywords: list[str] = []
    for value in meta.get("citation_keywords", []):
        keywords.extend(_split_keywords(value))
    _set(row, "dc_subject", keywords)
    journal = meta.get("citation_journal_title", [])
    doi = meta.get("citation_doi", [])
    source = journal + [f"doi:{value}" for value in doi if not value.lower().startswith("doi:")]
    source += [value for value in doi if value.lower().startswith("doi:")]
    _set(row, "dc_source", [" ".join(source)] if source else [])


def _apply_open_graph(row: dict[str, str], meta: dict[str, list[str]]) -> None:
    _set(row, "dc_title", meta.get("og:title", []))
    _set(row, "dc_description", [_clip(value) for value in meta.get("og:description", [])])
    _set(row, "dc_creator", meta.get("article:author", []))
    _set(row, "dc_publisher", meta.get("og:site_name", []))
    published = [_date_value(value) for value in meta.get("article:published_time", [])]
    _set(row, "dcterms_issued", published)
    tags: list[str] = []
    for value in meta.get("article:tag", []):
        tags.extend(_split_keywords(value) if "," in value else [value])
    _set(row, "dc_subject", tags)
    image = ""
    for key in ("og:image", "og:image:url", "twitter:image"):
        for value in meta.get(key, []):
            image = _image_url(value)
            if image:
                break
        if image:
            break
    if image:
        _set(row, "media_url", image)


def _apply_html(row: dict[str, str], soup: BeautifulSoup, meta: dict[str, list[str]]) -> None:
    if not row["dc_title"] and soup.title:
        row["dc_title"] = _plain(soup.title.get_text(" ", strip=True))
    _set(row, "dc_description", [_clip(value) for value in meta.get("description", [])])
    _set(row, "dc_creator", meta.get("author", []))
    keywords: list[str] = []
    for value in meta.get("keywords", []):
        keywords.extend(_split_keywords(value))
    _set(row, "dc_subject", keywords)
    language = ""
    root = soup.find("html")
    if root and root.get("lang"):
        language = str(root["lang"])
    if language:
        _set(row, "dc_language", language)


def _apply_dc(row: dict[str, str], meta: dict[str, list[str]], contributors: list[str]) -> None:
    for key, column in _DC_FIELDS.items():
        values = meta.get(key, [])
        if not values:
            continue
        if column == "dc_description":
            values = [_clip(value) for value in values]
        _set(row, column, values, replace=True)
    contributors.extend(meta.get("dc.contributor", []))
    contributors.extend(meta.get("dcterms.contributor", []))


def _apply_license(row: dict[str, str], soup: BeautifulSoup, page_url: str) -> None:
    for link in soup.find_all("link", href=True):
        if "license" not in _rels(link):
            continue
        url = _abs(page_url, link["href"])
        if url and url not in row["dc_rights"]:
            row["dc_rights"] = f"{row['dc_rights']} {url}".strip()


def _normalize_language(row: dict[str, str]) -> None:
    if not row["dc_language"]:
        return
    mapped = [_language(part) for part in re.split(r"\s*\|\s*|,\s*", row["dc_language"])]
    row["dc_language"] = MULTI_VALUE.join(_unique(mapped))


def _backfill_dates(row: dict[str, str]) -> None:
    if not row["dc_date"]:
        row["dc_date"] = row["dcterms_issued"] or row["dcterms_created"]
    if not row["dcterms_temporal"]:
        row["dcterms_temporal"] = row["dcterms_issued"] or row["dcterms_created"]
    if not row["dcterms_temporal"] and row["dc_date"] and len(row["dc_date"]) <= 40:
        row["dcterms_temporal"] = row["dc_date"]
    elif not row["dcterms_temporal"]:
        row["dcterms_temporal"] = _coverage_years(f"{row['dc_title']} {row['dc_description']}")


def _backfill_coverage(row: dict[str, str]) -> None:
    if not row["dcterms_spatial"]:
        row["dcterms_spatial"] = find_places(f"{row['dc_title']} {row['dc_description']}")
    if not row["dc_coverage"]:
        row["dc_coverage"] = "; ".join(
            part for part in (row["dcterms_spatial"], row["dcterms_temporal"]) if part
        )


def _prefer_page_url(row: dict[str, str], soup: BeautifulSoup, page_url: str) -> list[str]:
    """Keep a resolvable URL in dc_identifier. Any other identifier is returned for credit."""
    others: list[str] = []
    canonical = ""
    for link in soup.find_all("link", href=True):
        if "canonical" in _rels(link):
            canonical = _abs(page_url, link["href"])
            break
    chosen = page_url
    if canonical and _same_site(canonical, page_url):
        chosen = canonical
    elif canonical:
        others.append(canonical)
    current = row["dc_identifier"]
    if current and _key(current) != _key(chosen):
        others.append(current)
    row["dc_identifier"] = chosen
    return _unique(others)


def _remember_other_ids(row: dict[str, str], other_ids: list[str]) -> None:
    if not other_ids:
        return
    extra = " ".join(other_ids)
    if not row["dc_source"]:
        row["dc_source"] = extra
    elif extra not in row["dc_source"]:
        row["dc_source"] = f"{row['dc_source']} ({extra})"


def _primary(nodes: list[dict], page_url: str) -> dict | None:
    best: dict | None = None
    best_score = -1
    page_key = _key(page_url)
    for node in nodes:
        types = _type_tokens(node)
        if types and types <= _SKIP_TYPES | {"listitem", "itemlist"}:
            continue
        url = _node_url(node)
        if url and _key(url) != page_key:
            continue
        score = 1
        if types & {"article", "newsarticle", "scholarlyarticle", "blogposting", "book", "report"}:
            score = 3
        elif types & _WORK_TYPES:
            score = 2
        if score > best_score:
            best = node
            best_score = score
    return best


def _jsonld_links(nodes: list[dict], page_url: str) -> list[Link]:
    links: list[Link] = []
    page_key = _key(page_url)
    for node in nodes:
        types = _type_tokens(node)
        if types and types <= _SKIP_TYPES:
            continue
        if types and not (types & _WORK_TYPES):
            continue
        url = _node_url(node)
        if not url or _key(url) == page_key or _skip_url(url):
            continue
        mime = _mime_for(url)
        names = _text_list(node.get("headline") or node.get("name"))
        links.append(
            Link(
                url,
                names[0] if names else "",
                page_url,
                _same_site(url, page_url) and not mime,
                mime,
            )
        )
    return links


def _jsonld_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []
    for script in soup.find_all("script"):
        kind = script.get("type") or ""
        if isinstance(kind, list):
            kind = " ".join(kind)
        if "ld+json" not in kind.lower():
            continue
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes.extend(_flatten_jsonld(data))
    return nodes


def _flatten_jsonld(data: object, depth: int = 0) -> list[dict]:
    if depth > 8:
        return []
    if isinstance(data, list):
        nodes: list[dict] = []
        for item in data:
            nodes.extend(_flatten_jsonld(item, depth + 1))
        return nodes
    if not isinstance(data, dict):
        return []
    if "breadcrumblist" in _type_tokens(data):
        return []
    nodes = [data]
    for key in _JSON_KEYS:
        if key in data:
            nodes.extend(_flatten_jsonld(data[key], depth + 1))
    return nodes


def _is_listing(soup: BeautifulSoup, nodes: list[dict], root, page_url: str) -> bool:
    for node in nodes:
        types = _type_tokens(node)
        if types & {"itemlist", "collectionpage", "searchresultspage"}:
            return True
    text = root.get_text(" ", strip=True) if root else ""
    if len(_list_links(root)) >= 5 and len(text) < 4000:
        return True
    return bool(_next_urls(soup, page_url) and len(_list_links(root)) >= 5)


def _content_anchors(root) -> list:
    if root is None:
        return []
    return [anchor for anchor in root.find_all("a", href=True) if not _in_chrome(anchor)]


def _list_links(root) -> list:
    found = []
    if root is None:
        return found
    for listing in root.find_all(["ul", "ol"]):
        if _in_chrome(listing):
            continue
        anchors = [anchor for anchor in listing.find_all("a", href=True) if not _in_chrome(anchor)]
        if len(anchors) >= 4:
            found.extend(anchors)
    return found


def _heading_links(root) -> list:
    if root is None:
        return []
    return [
        anchor
        for anchor in root.find_all("a", href=True)
        if anchor.find_parent(["h2", "h3", "h4"]) is not None and not _in_chrome(anchor)
    ]


def _card_links(root) -> list:
    found = []
    if root is None:
        return found
    for anchor in root.find_all("a", href=True):
        if _in_chrome(anchor):
            continue
        if _CARD.search(_class_text(anchor)) or any(
            _CARD.search(_class_text(parent)) for parent in list(anchor.parents)[:3]
        ):
            found.append(anchor)
    return found


def _bibliography_links(soup: BeautifulSoup) -> list:
    found = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        label = heading.get_text(" ", strip=True)
        if not _BIB.search(label):
            continue
        level = heading.name
        for sibling in heading.find_next_siblings():
            if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4"} and sibling.name <= level:
                break
            found.extend(sibling.find_all("a", href=True))
    return found


def _next_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    found: list[str] = []
    for tag in soup.find_all(["a", "link"], href=True):
        if "next" not in _rels(tag):
            continue
        url = _abs(page_url, tag["href"])
        if url and _same_site(url, page_url):
            found.append(url)
    return found


def _in_chrome(tag) -> bool:
    for parent in tag.parents:
        name = getattr(parent, "name", None)
        if name in {"nav", "header", "footer", "aside"}:
            return True
        role = parent.get("role") if hasattr(parent, "get") else None
        if isinstance(role, list):
            role = " ".join(role)
        if isinstance(role, str) and role.lower() in {"navigation", "banner", "contentinfo"}:
            return True
    return False


def _class_text(tag) -> str:
    classes = tag.get("class") if hasattr(tag, "get") else None
    if not classes:
        return ""
    if isinstance(classes, str):
        return classes
    return " ".join(classes)


async def _load_robots(client: httpx.AsyncClient, url: str) -> RobotFileParser:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        response = await client.get(robots_url, timeout=15.0)
        if response.status_code == 200:
            parser.parse(response.text.splitlines())
        else:
            parser.parse([])
    except Exception:
        parser.parse([])
    return parser


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            async with client.stream("GET", url) as response:
                if response.status_code in {429, 500, 502, 503, 504}:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                ctype = response.headers.get("content-type", "")
                final_url = str(response.url)
                if not _is_markup(ctype):
                    return ctype, "", final_url
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) >= 2_000_000:
                        break
            text = bytes(data).decode("utf-8", "replace")
            if "atom_challenge" in text or "JavaScript is required for this step" in text:
                raise HarvestError(f"site returned a bot challenge for {url}")
            return ctype, text, final_url
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            last_error = exc
            await asyncio.sleep(1.0 * (attempt + 1))
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(1.0 * (attempt + 1))
    raise HarvestError(f"failed to fetch {url}: {last_error}")


def _meta_map(soup: BeautifulSoup) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").strip().lower()
        content = tag.get("content")
        if not key or content is None:
            continue
        text = _plain(str(content))
        if text:
            found.setdefault(key, []).append(text)
    return found


def _set(row: dict[str, str], key: str, values: list[str] | str, *, replace: bool = False) -> None:
    if isinstance(values, str):
        values = [values]
    cleaned = _unique([_plain(value) for value in values if _plain(value)])
    if not cleaned or (row.get(key) and not replace):
        return
    if key in _MULTI:
        row[key] = MULTI_VALUE.join(cleaned)
    elif key == "dc_description":
        row[key] = _clip(cleaned[0])
    else:
        row[key] = cleaned[0]


def _text_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        cleaned = _plain(value)
        return [cleaned] if cleaned else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_text_list(item))
        return found
    if isinstance(value, dict):
        if value.get("name"):
            return _text_list(value["name"])
        given = _plain(str(value.get("givenName") or ""))
        family = _plain(str(value.get("familyName") or ""))
        full = _plain(f"{given} {family}")
        return [full] if full else []
    return []


def _date_value(value: object) -> str:
    values = _text_list(value)
    if not values:
        return ""
    text = values[0]
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
        return text[:10]
    return text


def _schema_type(node: dict) -> str:
    raw = node.get("@type") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ""
    names = [item.rstrip("/").split("/")[-1] for item in raw if isinstance(item, str)]
    for name in reversed(names):
        mapped = _TYPE_MAP.get(name.lower())
        if mapped:
            return mapped
    return names[-1] if names else ""


def _type_tokens(node: dict) -> set[str]:
    raw = node.get("@type") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {item.rstrip("/").split("/")[-1].lower() for item in raw if isinstance(item, str)}


def _node_url(node: dict) -> str:
    for key in ("url", "@id", "contentUrl"):
        found = _first_http(node.get(key))
        if found:
            return found
    item = node.get("item")
    if isinstance(item, dict):
        return _node_url(item)
    if isinstance(item, str) and item.startswith(("http://", "https://")):
        return item
    return ""


def _first_http(value: object, depth: int = 0) -> str:
    if depth > 5 or value is None:
        return ""
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, list):
        for item in value:
            found = _first_http(item, depth + 1)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "@id", "contentUrl"):
            found = _first_http(value.get(key), depth + 1)
            if found:
                return found
    return ""


def _image_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        return ""
    path = urlsplit(value).path.lower()
    if path.endswith((".html", ".htm", ".php", ".pdf")):
        return ""
    return value


def _language(value: str) -> str:
    value = value.strip().replace("_", "-")
    if not value:
        return ""
    lower = value.lower()
    if lower in _LANG:
        return _LANG[lower]
    primary = lower.split("-")[0]
    if primary in _LANG:
        return _LANG[primary]
    if len(lower) == 3 and lower.isalpha():
        return lower
    return lower


def _party(url: str, site: str, home: str) -> tuple[str, str]:
    if _same_site(url, home):
        return site, site
    host = _host(url)
    return host, host


def _type_for_mime(mime: str) -> str:
    if not mime or mime == "text/html":
        return "Document"
    if mime.startswith("image/"):
        return "Photograph"
    if mime.startswith("audio/"):
        return "Oral history"
    if mime.startswith("video/"):
        return "Video"
    return "Document"


def _mime_for(url: str) -> str:
    path = urlsplit(url).path.lower()
    for extension, mime in _FILE_TYPES.items():
        if path.endswith(extension):
            return mime
    return ""


def _mime_from_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_markup(content_type: str) -> bool:
    lowered = content_type.lower()
    return "html" in lowered or "xml" in lowered or lowered.startswith("text/")


def _abs(page: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
        return ""
    absolute = urljoin(page, href)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _key(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ],
        doseq=True,
    )
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _same_site(left: str, right: str) -> bool:
    return _host(left) == _host(right)


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def _skip_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return True
    if _SKIP_PATH.search(parts.path or "/"):
        return True
    return bool(_SHARE.search(url))


def _rels(tag) -> set[str]:
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {part.lower() for part in rel}


def _link_title(anchor, url: str) -> str:
    text = _plain(anchor.get_text(" ", strip=True))
    if text.lower() in _GENERIC_TITLES or len(text) < 3:
        return _title_from_url(url)
    return text


def _title_from_url(url: str) -> str:
    segment = urlsplit(url).path.rstrip("/").split("/")[-1]
    segment = re.sub(r"\.(html?|php|pdf|docx?|pptx?|xlsx?)$", "", segment, flags=re.I)
    segment = segment.replace("-", " ").replace("_", " ").strip()
    return segment or url


def _split_keywords(value: str) -> list[str]:
    return [part.strip() for part in re.split(r",|;", value) if part.strip()]


def _clip(value: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    value = _plain(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _unique(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
    return found


def _sentence(current: str, addition: str) -> str:
    if not current:
        return addition
    if addition in current:
        return current
    return current.rstrip(". ") + ". " + addition
