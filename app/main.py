from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.db import get_connection, init_db
from app.sources.arxiv import fetch_arxiv, normalize_arxiv_id, search_arxiv_by_author
from app.sources.lesswrong import search_lesswrong
from app.sources.registry import fetch_by_input, normalize_source_id

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

BATCH_IMPORT_REQUEST_SPACING_SECONDS = 0.8


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ImportArxivIn(BaseModel):
    value: str = Field(min_length=1)


class ImportArxivBatchIn(BaseModel):
    values: list[str] = Field(min_length=1)


class ImportSourceIn(BaseModel):
    value: str = Field(min_length=1)


class ImportArxivResultIn(BaseModel):
    arxiv_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    published_at: str | None = None
    arxiv_url: str | None = None


class ImportArxivSearchBatchIn(BaseModel):
    papers: list[ImportArxivResultIn] = Field(min_length=1)


class PaperPatchIn(BaseModel):
    status: str | None = None
    project_id: int | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    starred: bool | None = None
    notes: str | None = None


class PaperReorderIn(BaseModel):
    paper_ids: list[int] = Field(min_length=1)


app = FastAPI(title="Vibe Paper Stack")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


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
        "sort_order": row["sort_order"],
        "tags": [{"id": tag["id"], "name": tag["name"]} for tag in tags],
    }


def upsert_paper(conn: sqlite3.Connection, meta: dict[str, Any]) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO papers (arxiv_id, title, abstract, authors_json, categories_json, published_at, arxiv_url, sort_order)
        VALUES (
            :arxiv_id,
            :title,
            :abstract,
            :authors_json,
            :categories_json,
            :published_at,
            :arxiv_url,
            COALESCE(:sort_order, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM papers))
        )
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
            "sort_order": meta.get("sort_order"),
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


@app.post("/api/papers/import-source")
async def import_source(payload: ImportSourceIn) -> dict[str, Any]:
    meta = await fetch_by_input(payload.value)

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

    metas: list[dict[str, Any]] = []
    for index, arxiv_id in enumerate(unique_ids):
        metas.append(await fetch_arxiv(arxiv_id))
        if index < len(unique_ids) - 1:
            # arXiv rejects bursty request patterns; small spacing avoids 429 for batch add.
            await asyncio.sleep(BATCH_IMPORT_REQUEST_SPACING_SECONDS)

    conn = get_connection()
    papers: list[dict[str, Any]] = []
    with conn:
        for meta in metas:
            row = upsert_paper(conn, meta)
            papers.append(row_to_paper(conn, row))
    conn.close()
    return papers


@app.post("/api/papers/import-from-search")
def import_from_search(payload: ImportArxivSearchBatchIn) -> list[dict[str, Any]]:
    metas: list[dict[str, Any]] = []
    for paper in payload.papers:
        source_id = normalize_source_id(paper.arxiv_id)
        metas.append(
            {
                "arxiv_id": source_id,
                "title": paper.title.strip(),
                "abstract": paper.abstract or "",
                "authors": [a for a in paper.authors if a],
                "categories": [c for c in paper.categories if c],
                "published_at": paper.published_at,
                "arxiv_url": paper.arxiv_url,
            }
        )

    conn = get_connection()
    papers: list[dict[str, Any]] = []
    with conn:
        for meta in metas:
            row = upsert_paper(conn, meta)
            papers.append(row_to_paper(conn, row))
    conn.close()
    return papers


@app.get("/api/search")
async def search_sources(
    q: str = Query(min_length=2),
    source: Literal["arxiv", "lesswrong"] = Query(default="arxiv"),
    max_results: int = Query(default=20, ge=1, le=50),
) -> list[dict[str, Any]]:
    if source == "lesswrong":
        results = await search_lesswrong(q, max_results)
    else:
        results = await search_arxiv_by_author(q, max_results)

    conn = get_connection()
    existing_rows = conn.execute("SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL").fetchall()
    conn.close()
    existing_ids = {row["arxiv_id"] for row in existing_rows}

    for item in results:
        item["already_added"] = item["arxiv_id"] in existing_ids
    return results


@app.get("/api/arxiv/search-by-author")
async def arxiv_search_by_author(
    author: str = Query(min_length=2),
    max_results: int = Query(default=20, ge=1, le=50),
) -> list[dict[str, Any]]:
    # Backward-compatible endpoint kept for existing clients.
    return await search_sources(q=author, source="arxiv", max_results=max_results)


@app.get("/api/papers")
def list_papers(
    q: str | None = None,
    status: str | None = None,
    project_id: int | None = None,
    project_none: bool = False,
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
    if project_none:
        clauses.append("p.project_id IS NULL")
    elif project_id:
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
        ORDER BY p.starred DESC, COALESCE(p.sort_order, 2147483647) ASC, p.created_at DESC
        """,
        params,
    ).fetchall()
    data = [row_to_paper(conn, row) for row in rows]
    conn.close()
    return data


@app.post("/api/papers/reorder")
def reorder_papers(payload: PaperReorderIn) -> dict[str, bool]:
    paper_ids = payload.paper_ids
    if len(set(paper_ids)) != len(paper_ids):
        raise HTTPException(status_code=400, detail="paper_ids must be unique")

    placeholders = ",".join(["?"] * len(paper_ids))

    conn = get_connection()
    rows = conn.execute(f"SELECT id, sort_order FROM papers WHERE id IN ({placeholders})", paper_ids).fetchall()
    if len(rows) != len(paper_ids):
        conn.close()
        raise HTTPException(status_code=404, detail="One or more papers were not found")

    min_sort_order = min((row["sort_order"] for row in rows if row["sort_order"] is not None), default=1)

    with conn:
        for index, paper_id in enumerate(paper_ids):
            conn.execute(
                "UPDATE papers SET sort_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (min_sort_order + index, paper_id),
            )
    conn.close()
    return {"ok": True}


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
