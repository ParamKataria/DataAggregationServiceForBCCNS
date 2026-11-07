"""Fetch exhibit listings and item pages, then return Dublin Core rows."""

from __future__ import annotations

import asyncio
import sys

import httpx

from scraper.extra import harvest_extra
from scraper.parse import parse_item, parse_result_page
from scraper.sources import SOURCES, USER_AGENT, Source


class HarvestError(RuntimeError):
    pass


async def harvest_all(limit_per_collection: int = 0, only: str = "all") -> list[dict[str, str]]:
    timeout = httpx.Timeout(30.0)
    limits = httpx.Limits(max_connections=6, max_keepalive_connections=6)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    semaphore = asyncio.Semaphore(4)

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        limits=limits,
    ) as client:

        async def fetch(url: str) -> str:
            async with semaphore:
                text = await _fetch_text(client, url)
                await asyncio.sleep(0.12)
                return text

        records: list[dict[str, str]] = []
        if only in {"all", "nsa"}:
            listings = await asyncio.gather(
                *[_collect_ids(fetch, source) for source in SOURCES]
            )
            jobs: list[tuple[Source, int]] = []
            for source, item_ids in zip(SOURCES, listings, strict=True):
                chosen = item_ids if limit_per_collection <= 0 else item_ids[:limit_per_collection]
                print(f"{source.collection}: {len(chosen)} of {len(item_ids)} records", file=sys.stderr)
                jobs.extend((source, item_id) for item_id in chosen)

            done = 0

            async def one(source: Source, item_id: int) -> dict[str, str] | None:
                nonlocal done
                url = source.item_url(item_id)
                try:
                    html = await fetch(url)
                    record = parse_item(html, url, source)
                except Exception as exc:
                    print(f"skip {url}: {exc}", file=sys.stderr)
                    record = None
                done += 1
                if done % 25 == 0 or done == len(jobs):
                    print(f"fetched {done}/{len(jobs)}", file=sys.stderr)
                return record

            nsa_records = await asyncio.gather(*(one(source, item_id) for source, item_id in jobs))
            records.extend(record for record in nsa_records if record)

        if only in {"all", "extra"}:
            records.extend(await harvest_extra(client, semaphore, limit_per_collection))

    return records


async def _collect_ids(fetch, source: Source) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    start = 1
    while True:
        html = await fetch(source.results_url(start))
        page_ids, last_shown, total = parse_result_page(html)
        if not page_ids:
            break
        for item_id in page_ids:
            if item_id not in seen:
                seen.add(item_id)
                found.append(item_id)
        if last_shown is None or total is None or last_shown >= total:
            break
        next_start = last_shown + 1
        if next_start <= start:
            break
        start = next_start
    return found


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = await client.get(url)
            if response.status_code in {429, 500, 502, 503, 504}:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            text = response.text
            if "atom_challenge" in text or "JavaScript is required for this step" in text:
                raise HarvestError(f"site returned a bot challenge for {url}")
            return text
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(1.0 * (attempt + 1))
    raise HarvestError(f"failed to fetch {url}: {last_error}")
