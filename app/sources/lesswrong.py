from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

LESSWRONG_POST_PATH_PATTERN = re.compile(r"^posts/([A-Za-z0-9_-]+)")
LESSWRONG_URL_PATTERN = re.compile(r"lesswrong\.com/posts/([A-Za-z0-9_-]+)")
LESSWRONG_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
LESSWRONG_HEADERS = {"User-Agent": "vibe-paper-stack/1.0 (mailto:local@localhost)"}


def extract_lesswrong_post_id(value: str) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None

    match = LESSWRONG_URL_PATTERN.search(cleaned)
    if match:
        return match.group(1)

    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    if "lesswrong.com" not in parsed.netloc:
        return None

    path = parsed.path.strip("/")
    path_match = LESSWRONG_POST_PATH_PATTERN.match(path)
    if path_match:
        return path_match.group(1)

    return None


def normalize_lesswrong_post_id(value: str) -> str:
    post_id = extract_lesswrong_post_id(value)
    if not post_id:
        raise HTTPException(status_code=400, detail="Could not parse LessWrong post id from input")
    return post_id


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


async def _fetch_feed_root() -> ET.Element:
    async with httpx.AsyncClient(timeout=LESSWRONG_TIMEOUT, headers=LESSWRONG_HEADERS) as client:
        response = await client.get("https://www.lesswrong.com/feed.xml")

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Unable to reach LessWrong right now")

    try:
        return ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from LessWrong") from exc


async def _fetch_from_json(post_id: str) -> dict[str, Any]:
    url = f"https://www.lesswrong.com/posts/{post_id}.json"
    async with httpx.AsyncClient(timeout=LESSWRONG_TIMEOUT, headers=LESSWRONG_HEADERS) as client:
        response = await client.get(url)

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Post not found on LessWrong")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Unable to reach LessWrong right now")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from LessWrong") from exc

    post = payload.get("post") if isinstance(payload, dict) else None
    if not isinstance(post, dict):
        raise HTTPException(status_code=502, detail="Invalid response from LessWrong")

    title = (post.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=502, detail="Invalid response from LessWrong")

    excerpt = post.get("excerpt") or ""
    html_body = ""
    contents = post.get("contents")
    if isinstance(contents, dict):
        html_body = contents.get("html") or ""

    summary = excerpt.strip() or _strip_html(html_body) or "(No summary available)"
    user = post.get("user") if isinstance(post.get("user"), dict) else {}
    author_name = (user.get("displayName") or user.get("username") or "LessWrong").strip()
    link = post.get("url") or f"https://www.lesswrong.com/posts/{post_id}"
    published_at = post.get("postedAt") or post.get("createdAt")

    return {
        "arxiv_id": f"lw:{post_id}",
        "title": title,
        "abstract": summary,
        "authors": [author_name] if author_name else ["LessWrong"],
        "categories": ["lesswrong"],
        "published_at": published_at,
        "arxiv_url": link,
    }


async def _fetch_from_feed(post_id: str) -> dict[str, Any]:
    root = await _fetch_feed_root()
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        if post_id not in link:
            continue

        title = (item.findtext("title") or "").strip()
        description = _strip_html(item.findtext("description") or "") or "(No summary available)"
        author = (item.findtext("dc:creator", default="LessWrong", namespaces=ns) or "LessWrong").strip()
        published_at = item.findtext("pubDate")
        return {
            "arxiv_id": f"lw:{post_id}",
            "title": title or f"LessWrong post {post_id}",
            "abstract": description,
            "authors": [author],
            "categories": ["lesswrong"],
            "published_at": published_at,
            "arxiv_url": link or f"https://www.lesswrong.com/posts/{post_id}",
        }

    raise HTTPException(status_code=404, detail="Post not found on LessWrong")


async def fetch_lesswrong(value: str) -> dict[str, Any]:
    post_id = normalize_lesswrong_post_id(value)

    try:
        return await _fetch_from_json(post_id)
    except HTTPException as exc:
        # LessWrong can block automated direct post fetches (e.g. 429). Fall back to RSS metadata.
        if exc.status_code in {502, 429}:
            return await _fetch_from_feed(post_id)
        raise


async def search_lesswrong(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    query_text = (query or "").strip().lower()
    if len(query_text) < 2:
        raise HTTPException(status_code=400, detail="Search query must be at least 2 characters")

    root = await _fetch_feed_root()
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}

    results: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        description_raw = item.findtext("description") or ""
        description = _strip_html(description_raw)
        author = (item.findtext("dc:creator", default="LessWrong", namespaces=ns) or "LessWrong").strip()
        link = (item.findtext("link") or "").strip()

        haystack = f"{title} {description} {author}".lower()
        if query_text not in haystack:
            continue

        post_id = extract_lesswrong_post_id(link)
        if not post_id:
            continue

        results.append(
            {
                "arxiv_id": f"lw:{post_id}",
                "title": title or f"LessWrong post {post_id}",
                "abstract": description or "(No summary available)",
                "authors": [author],
                "categories": ["lesswrong"],
                "published_at": item.findtext("pubDate"),
                "arxiv_url": link or f"https://www.lesswrong.com/posts/{post_id}",
            }
        )

        if len(results) >= max_results:
            break

    return results
