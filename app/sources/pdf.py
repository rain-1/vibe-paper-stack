from __future__ import annotations

import asyncio
import hashlib
import re
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from fastapi import HTTPException
from pypdf import PdfReader

PDF_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
PDF_HEADERS = {"User-Agent": "vibe-paper-stack/1.0 (mailto:local@localhost)"}
MAX_PDF_BYTES = 20 * 1024 * 1024


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


def _extract_first_page_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    if not reader.pages:
        return ""

    text = reader.pages[0].extract_text() or ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _guess_title(lines: list[str], fallback_title: str) -> str:
    for line in lines[:12]:
        if line.lower().startswith("abstract"):
            continue
        if len(line) < 8 or len(line) > 220:
            continue
        if re.search(r"\b(arxiv|doi|http|www\.)\b", line, flags=re.IGNORECASE):
            continue
        if sum(ch.isalpha() for ch in line) < 6:
            continue
        return line
    return fallback_title


def _extract_abstract(text: str) -> str | None:
    if not text:
        return None

    abstract_start = re.search(r"\babstract\b\s*[:\-.]?\s*", text, flags=re.IGNORECASE)
    if not abstract_start:
        return None

    start = abstract_start.end()
    remainder = text[start:]
    stop = re.search(
        r"\n\s*(?:1\.?\s+introduction|introduction\b|keywords\b|contents\b|i\.?\s+introduction)",
        remainder,
        flags=re.IGNORECASE,
    )
    abstract = remainder[: stop.start()] if stop else remainder
    abstract = re.sub(r"\s+", " ", abstract).strip()

    if len(abstract) < 40:
        return None

    return abstract[:2200]


def _extract_pdf_metadata(pdf_bytes: bytes, fallback_title: str) -> tuple[str, str]:
    try:
        text = _extract_first_page_text(pdf_bytes)
    except Exception:
        return fallback_title, "Imported from direct PDF URL"

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]

    title = _guess_title(lines, fallback_title)
    abstract = _extract_abstract(text) or "Imported from direct PDF URL"
    return title, abstract


def _ensure_size_within_limit(content_length_header: str | None) -> None:
    if not content_length_header:
        return

    try:
        content_length = int(content_length_header)
    except ValueError:
        return

    if content_length > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail=f"PDF is too large (max {MAX_PDF_BYTES // (1024 * 1024)} MB)")


async def _download_pdf_bytes(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    total = 0

    async with client.stream("GET", url) as response:
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="Unable to download PDF right now")

        _ensure_size_within_limit(response.headers.get("content-length"))

        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise HTTPException(status_code=413, detail=f"PDF is too large (max {MAX_PDF_BYTES // (1024 * 1024)} MB)")
            chunks.append(chunk)

        return b"".join(chunks), str(response.url)


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

        _ensure_size_within_limit(response.headers.get("content-length"))

        content_type = (response.headers.get("content-type") or "").lower()
        final_url = str(response.url)
        if "pdf" not in content_type and not urlparse(final_url).path.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="URL does not appear to be a PDF")

        pdf_bytes, final_url = await _download_pdf_bytes(client, final_url)

    doc_id = hashlib.sha1(final_url.encode("utf-8")).hexdigest()[:16]
    fallback_title = _title_from_url(final_url)
    title, abstract = await asyncio.to_thread(_extract_pdf_metadata, pdf_bytes, fallback_title)

    return {
        "arxiv_id": f"pdf:{doc_id}",
        "title": title,
        "abstract": abstract,
        "authors": ["Unknown"],
        "categories": ["pdf"],
        "published_at": None,
        "arxiv_url": final_url,
    }
