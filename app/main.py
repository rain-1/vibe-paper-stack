from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.db import get_connection, init_db

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_URL_PATTERN = re.compile(r"arxiv\.org/(abs|pdf)/([^?#]+)")


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ImportArxivIn(BaseModel):
    value: str = Field(min_length=1)


class ImportArxivBatchIn(BaseModel):
    values: list[str] = Field(min_length=1)


class PaperPatchIn(BaseModel):
    status: str | None = None
    project_id: int | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    starred: bool | None = None
    notes: str | None = None


app = FastAPI(title="Vibe Paper Stack")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


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


def row_to_paper(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    tags = conn.execute(
        """
        SELECT t.id, t.name
        FROM tags t
        JOIN paper_tags pt ON pt.tag_id = t.id
        WHERE pt.paper_id = ?
        ORDER BY t.name COLLATE NOCASE
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "arxiv_id": row["arxiv_id"],
        "title": row["title"],
        "abstract": row["abstract"],
        "authors": json.loads(row["authors_json"]),
        "categories": json.loads(row["categories_json"]),
        "published_at": row["published_at"],
        "arxiv_url": row["arxiv_url"],
        "status": row["status"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "rating": row["rating"],
        "starred": bool(row["starred"]),
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": [{"id": tag["id"], "name": tag["name"]} for tag in tags],
    }


async def fetch_arxiv(arxiv_id: str) -> dict[str, Any]:
    url = "https://export.arxiv.org/api/query"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params={"id_list": arxiv_id})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Unable to reach arXiv right now") from exc

    try:
        root = ET.fromstring(response.text)
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
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f'au:"{author}"',
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Unable to reach arXiv right now") from exc

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from arXiv") from exc
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        raw_id = entry.findtext("atom:id", default="", namespaces=ns)
        arxiv_id = extract_arxiv_id(raw_id)
        if not arxiv_id:
            continue
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        published = entry.findtext("atom:published", default=None, namespaces=ns)
        authors = [
            (author_el.findtext("atom:name", default="", namespaces=ns) or "").strip()
            for author_el in entry.findall("atom:author", ns)
        ]
        categories = [cat.get("term", "") for cat in entry.findall("atom:category", ns)]
        link = f"https://arxiv.org/abs/{arxiv_id}"
        results.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": summary,
                "authors": [a for a in authors if a],
                "categories": [c for c in categories if c],
                "published_at": published,
                "arxiv_url": link,
            }
        )
    return results


def upsert_paper(conn: sqlite3.Connection, meta: dict[str, Any]) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO papers (arxiv_id, title, abstract, authors_json, categories_json, published_at, arxiv_url)
        VALUES (:arxiv_id, :title, :abstract, :authors_json, :categories_json, :published_at, :arxiv_url)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title=excluded.title,
            abstract=excluded.abstract,
            authors_json=excluded.authors_json,
            categories_json=excluded.categories_json,
            published_at=excluded.published_at,
            arxiv_url=excluded.arxiv_url,
            updated_at=CURRENT_TIMESTAMP
        """,
        {
            **meta,
            "authors_json": json.dumps(meta["authors"]),
            "categories_json": json.dumps(meta["categories"]),
        },
    )
    return conn.execute(
        """
        SELECT p.*, pr.name AS project_name
        FROM papers p
        LEFT JOIN projects pr ON pr.id = p.project_id
        WHERE p.arxiv_id = ?
        """,
        (meta["arxiv_id"],),
    ).fetchone()


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/projects")
def get_projects() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("SELECT id, name, created_at FROM projects ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.post("/api/projects")
def create_project(payload: ProjectIn) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("INSERT INTO projects (name) VALUES (?)", (payload.name.strip(),))
        row = conn.execute("SELECT id, name, created_at FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Project already exists")
    finally:
        conn.close()


@app.get("/api/tags")
def get_tags() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("SELECT id, name, created_at FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.post("/api/tags")
def create_tag(payload: TagIn) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (payload.name.strip(),))
        row = conn.execute("SELECT id, name, created_at FROM tags WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Tag already exists")
    finally:
        conn.close()


@app.post("/api/papers/import-arxiv")
async def import_arxiv(payload: ImportArxivIn) -> dict[str, Any]:
    arxiv_id = normalize_arxiv_id(payload.value)
    meta = await fetch_arxiv(arxiv_id)

    conn = get_connection()
    with conn:
        row = upsert_paper(conn, meta)
    paper = row_to_paper(conn, row)
    conn.close()
    return paper


@app.post("/api/papers/import-arxiv-batch")
async def import_arxiv_batch(payload: ImportArxivBatchIn) -> list[dict[str, Any]]:
    normalized_ids = []
    for value in payload.values:
        normalized_ids.append(normalize_arxiv_id(value))

    unique_ids = list(dict.fromkeys(normalized_ids))
    metas = await asyncio.gather(*[fetch_arxiv(arxiv_id) for arxiv_id in unique_ids])

    conn = get_connection()
    papers: list[dict[str, Any]] = []
    with conn:
        for meta in metas:
            row = upsert_paper(conn, meta)
            papers.append(row_to_paper(conn, row))
    conn.close()
    return papers


@app.get("/api/arxiv/search-by-author")
async def arxiv_search_by_author(author: str = Query(min_length=2), max_results: int = Query(default=20, ge=1, le=50)) -> list[dict[str, Any]]:
    results = await search_arxiv_by_author(author, max_results)
    conn = get_connection()
    existing_rows = conn.execute("SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL").fetchall()
    conn.close()
    existing_ids = {row["arxiv_id"] for row in existing_rows}

    for item in results:
        item["already_added"] = item["arxiv_id"] in existing_ids
    return results


@app.get("/api/papers")
def list_papers(
    q: str | None = None,
    status: str | None = None,
    project_id: int | None = None,
    tag: str | None = None,
    starred: bool | None = None,
    rating_min: int | None = Query(default=None, ge=1, le=5),
    include_done: bool = False,
) -> list[dict[str, Any]]:
    conn = get_connection()
    clauses: list[str] = []
    params: list[Any] = []

    if not include_done:
        clauses.append("p.status != 'done'")
    if status:
        clauses.append("p.status = ?")
        params.append(status)
    if project_id:
        clauses.append("p.project_id = ?")
        params.append(project_id)
    if starred is not None:
        clauses.append("p.starred = ?")
        params.append(int(starred))
    if rating_min is not None:
        clauses.append("COALESCE(p.rating, 0) >= ?")
        params.append(rating_min)
    if q:
        clauses.append("(p.title LIKE ? OR p.abstract LIKE ? OR p.authors_json LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.paper_id = p.id AND t.name = ?)"
        )
        params.append(tag)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT p.*, pr.name AS project_name
        FROM papers p
        LEFT JOIN projects pr ON pr.id = p.project_id
        {where}
        ORDER BY p.starred DESC, p.created_at DESC
        """,
        params,
    ).fetchall()
    data = [row_to_paper(conn, row) for row in rows]
    conn.close()
    return data


@app.patch("/api/papers/{paper_id}")
def patch_paper(paper_id: int, payload: PaperPatchIn) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] not in {"queued", "reading", "done"}:
        raise HTTPException(status_code=400, detail="Invalid status")

    conn = get_connection()
    row = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Paper not found")

    if updates:
        sets = []
        params: list[Any] = []
        for key, value in updates.items():
            sets.append(f"{key} = ?")
            params.append(int(value) if key == "starred" else value)
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.append(paper_id)
        with conn:
            conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", params)

    full_row = conn.execute(
        """
        SELECT p.*, pr.name AS project_name
        FROM papers p
        LEFT JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?
        """,
        (paper_id,),
    ).fetchone()
    result = row_to_paper(conn, full_row)
    conn.close()
    return result


@app.post("/api/papers/{paper_id}/tags")
def add_paper_tag(paper_id: int, payload: TagIn) -> dict[str, Any]:
    conn = get_connection()
    paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not paper:
        conn.close()
        raise HTTPException(status_code=404, detail="Paper not found")

    with conn:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (payload.name.strip(),))
        tag = conn.execute("SELECT id, name FROM tags WHERE name = ?", (payload.name.strip(),)).fetchone()
        conn.execute("INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)", (paper_id, tag["id"]))

    conn.close()
    return {"paper_id": paper_id, "tag": dict(tag)}


@app.delete("/api/papers/{paper_id}/tags/{tag_id}")
def remove_paper_tag(paper_id: int, tag_id: int) -> dict[str, Any]:
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?", (paper_id, tag_id))
    conn.close()
    return {"ok": True}


@app.get("/")
def root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
