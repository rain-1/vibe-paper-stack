from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from fastapi import HTTPException

PDF_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
PDF_HEADERS = {"User-Agent": "vibe-paper-stack/1.0 (mailto:local@localhost)"}


def extract_pdf_url(value: str) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None

    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    if parsed.scheme not in {"http", "https"}:
        return None

    if parsed.path.lower().endswith(".pdf"):
        return parsed.geturl()

    return None


def normalize_pdf_url(value: str) -> str:
    normalized = extract_pdf_url(value)
    if not normalized:
        raise HTTPException(status_code=400, detail="Could not parse a direct PDF URL from input")
    return normalized


def _title_from_url(url: str) -> str:
    path_name = PurePosixPath(urlparse(url).path).name
    filename = unquote(path_name or "").strip()
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]

    cleaned = re.sub(r"[_-]+", " ", filename)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "PDF document"


async def fetch_pdf(value: str) -> dict[str, Any]:
    url = normalize_pdf_url(value)

    async with httpx.AsyncClient(timeout=PDF_TIMEOUT, headers=PDF_HEADERS, follow_redirects=True) as client:
        response = await client.head(url)
        if response.status_code in {405, 501}:
            response = await client.get(url, headers={"Range": "bytes=0-0"})

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="PDF not found at URL")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Unable to fetch PDF right now")

    content_type = (response.headers.get("content-type") or "").lower()
    final_url = str(response.url)
    if "pdf" not in content_type and not urlparse(final_url).path.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="URL does not appear to be a PDF")

    doc_id = hashlib.sha1(final_url.encode("utf-8")).hexdigest()[:16]
    title = _title_from_url(final_url)

    return {
        "arxiv_id": f"pdf:{doc_id}",
        "title": title,
        "abstract": "Imported from direct PDF URL",
        "authors": ["Unknown"],
        "categories": ["pdf"],
        "published_at": None,
        "arxiv_url": final_url,
    }
