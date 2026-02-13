from __future__ import annotations

import asyncio
import copy
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_URL_PATTERN = re.compile(r"arxiv\.org/(abs|pdf)/([^?#]+)")
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_HEADERS = {"User-Agent": "vibe-paper-stack/1.0 (mailto:local@localhost)"}
ARXIV_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
ARXIV_REQUEST_ATTEMPTS = 3
AUTHOR_SEARCH_CACHE_TTL_SECONDS = 900
AUTHOR_SEARCH_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}


def extract_arxiv_id(raw_value: str) -> str | None:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return None

    url_match = ARXIV_URL_PATTERN.search(cleaned)
    if url_match:
        cleaned = url_match.group(2).replace(".pdf", "").strip("/")

    cleaned = cleaned.split("?")[0].strip("/")

    id_match = ARXIV_ID_PATTERN.search(cleaned)
    if id_match:
        return id_match.group(1)

    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    path = parsed.path.strip("/")
    if path.startswith("abs/") or path.startswith("pdf/"):
        path = path.split("/", 1)[1]

    path = path.removesuffix(".pdf")
    if "/" in path:
        # Legacy arXiv IDs like cs/0112017v1 or math.GT/0309136v2
        return path.rsplit("v", 1)[0]

    id_match = ARXIV_ID_PATTERN.search(path)
    if id_match:
        return id_match.group(1)

    return None


def normalize_arxiv_id(value: str) -> str:
    normalized = extract_arxiv_id(value)
    if not normalized:
        raise HTTPException(status_code=400, detail="Could not parse arXiv id from input")
    return normalized


def is_arxiv_rate_limited(response: httpx.Response) -> bool:
    return response.status_code == 429 or "rate exceeded" in response.text.lower()


async def request_arxiv_api(params: dict[str, Any]) -> str:
    for attempt in range(1, ARXIV_REQUEST_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=ARXIV_TIMEOUT, headers=ARXIV_HEADERS) as client:
                response = await client.get(ARXIV_API_URL, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == ARXIV_REQUEST_ATTEMPTS:
                raise HTTPException(status_code=502, detail="Unable to reach arXiv right now") from exc
            await asyncio.sleep(0.5 * attempt)
            continue

        if is_arxiv_rate_limited(response):
            if attempt == ARXIV_REQUEST_ATTEMPTS:
                raise HTTPException(status_code=429, detail="arXiv rate limit exceeded. Please retry in a few seconds.")
            await asyncio.sleep(1.0 * attempt)
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if attempt == ARXIV_REQUEST_ATTEMPTS:
                raise HTTPException(status_code=502, detail="Unable to reach arXiv right now") from exc
            await asyncio.sleep(0.5 * attempt)
            continue

        return response.text

    raise HTTPException(status_code=502, detail="Unable to reach arXiv right now")


def author_search_cache_key(author: str, max_results: int) -> tuple[str, int]:
    return (author.strip().lower(), max_results)


def get_cached_author_results(author: str, max_results: int) -> list[dict[str, Any]] | None:
    cached = AUTHOR_SEARCH_CACHE.get(author_search_cache_key(author, max_results))
    if not cached:
        return None

    created_at, results = cached
    if (time.time() - created_at) > AUTHOR_SEARCH_CACHE_TTL_SECONDS:
        return None

    return copy.deepcopy(results)


def get_stale_author_results(author: str, max_results: int) -> list[dict[str, Any]] | None:
    cached = AUTHOR_SEARCH_CACHE.get(author_search_cache_key(author, max_results))
    if not cached:
        return None

    return copy.deepcopy(cached[1])


def set_cached_author_results(author: str, max_results: int, results: list[dict[str, Any]]) -> None:
    AUTHOR_SEARCH_CACHE[author_search_cache_key(author, max_results)] = (time.time(), copy.deepcopy(results))


async def fetch_arxiv(arxiv_id: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(await request_arxiv_api({"id_list": arxiv_id}))
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from arXiv") from exc

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise HTTPException(status_code=404, detail="Paper not found on arXiv")

    title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
    summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
    published = entry.findtext("atom:published", default=None, namespaces=ns)

    authors = [
        (author.findtext("atom:name", default="", namespaces=ns) or "").strip()
        for author in entry.findall("atom:author", ns)
    ]
    categories = [cat.get("term", "") for cat in entry.findall("atom:category", ns)]

    link = None
    for link_el in entry.findall("atom:link", ns):
        if link_el.get("rel") == "alternate":
            link = link_el.get("href")
            break

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": summary,
        "authors": [a for a in authors if a],
        "categories": [c for c in categories if c],
        "published_at": published,
        "arxiv_url": link or f"https://arxiv.org/abs/{arxiv_id}",
    }


async def search_arxiv_by_author(author: str, max_results: int = 20) -> list[dict[str, Any]]:
    cached_results = get_cached_author_results(author, max_results)
    if cached_results is not None:
        return cached_results

    params = {
        "search_query": f'au:"{author}"',
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    try:
        root = ET.fromstring(await request_arxiv_api(params))
    except HTTPException as exc:
        stale_results = get_stale_author_results(author, max_results)
        if stale_results is not None and exc.status_code in {429, 502}:
            return stale_results
        raise
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from arXiv") from exc

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        raw_id = entry.findtext("atom:id", default="", namespaces=ns)
        result_id = extract_arxiv_id(raw_id)
        if not result_id:
            continue

        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        published = entry.findtext("atom:published", default=None, namespaces=ns)
        authors = [
            (author_el.findtext("atom:name", default="", namespaces=ns) or "").strip()
            for author_el in entry.findall("atom:author", ns)
        ]
        categories = [cat.get("term", "") for cat in entry.findall("atom:category", ns)]

        results.append(
            {
                "arxiv_id": result_id,
                "title": title,
                "abstract": summary,
                "authors": [a for a in authors if a],
                "categories": [c for c in categories if c],
                "published_at": published,
                "arxiv_url": f"https://arxiv.org/abs/{result_id}",
            }
        )

    set_cached_author_results(author, max_results, results)
    return results
