from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.sources.arxiv import extract_arxiv_id, fetch_arxiv, normalize_arxiv_id
from app.sources.lesswrong import extract_lesswrong_post_id, fetch_lesswrong
from app.sources.pdf import extract_pdf_url, fetch_pdf


def detect_source(value: str) -> str:
    if extract_lesswrong_post_id(value):
        return "lesswrong"
    if extract_arxiv_id(value):
        return "arxiv"
    if extract_pdf_url(value):
        return "pdf"
    raise HTTPException(
        status_code=400,
        detail="Unsupported source. Use an arXiv ID/URL, LessWrong post URL, or direct PDF URL.",
    )


async def fetch_by_input(value: str) -> dict[str, Any]:
    source = detect_source(value)
    if source == "lesswrong":
        return await fetch_lesswrong(value)
    if source == "pdf":
        return await fetch_pdf(value)

    return await fetch_arxiv(normalize_arxiv_id(value))


def normalize_source_id(value: str) -> str:
    if value.startswith("lw:") or value.startswith("pdf:"):
        return value
    return normalize_arxiv_id(value)
