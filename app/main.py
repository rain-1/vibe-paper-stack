from __future__ import annotations

import asyncio
import os
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.db import get_conn, init_db
from app.sources.arxiv import fetch_arxiv, normalize_arxiv_id, search_arxiv_by_author
from app.sources.lesswrong import search_lesswrong
from app.sources.registry import fetch_by_input, normalize_source_id

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

BATCH_IMPORT_REQUEST_SPACING_SECONDS = 0.8
MAX_TAG_LOOKUP_BATCH = 500
DEFAULT_PAGE_LIMIT = 100
PATCH_ALLOWED_FIELDS = {"status", "project_id", "rating", "starred", "notes"}
DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"]
_ALLOWED_ORIGINS_RAW = os.getenv("VIBE_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [origin.strip() for origin in _ALLOWED_ORIGINS_RAW.split(",") if origin.strip()] or DEFAULT_ALLOWED_ORIGINS


def _escape_like(value: str) -> str:
    """Escape special LIKE characters so they match literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


class DataPaperIn(BaseModel):
    arxiv_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    published_at: str | None = None
    arxiv_url: str | None = None
    status: Literal["queued", "reading", "done"] = "queued"
    project: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    starred: bool = False
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    sort_order: int | None = None


class DataImportIn(BaseModel):
    version: int = 1
    projects: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    papers: list[DataPaperIn] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Vibe Paper Stack", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


def build_tags_by_paper_id(
    conn: sqlite3.Connection,
    paper_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not paper_ids:
        return {}

    tags_by_paper_id: dict[int, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}

    for start in range(0, len(paper_ids), MAX_TAG_LOOKUP_BATCH):
        batch = paper_ids[start:start + MAX_TAG_LOOKUP_BATCH]
        placeholders = ','.join(['?'] * len(batch))
        rows = conn.execute(
            f'''
            SELECT pt.paper_id, t.id, t.name
            FROM paper_tags pt
            JOIN tags t ON t.id = pt.tag_id
            WHERE pt.paper_id IN ({placeholders})
            ORDER BY t.name COLLATE NOCASE
            ''',
            batch,
        ).fetchall()

        for row in rows:
            tags_by_paper_id[row['paper_id']].append({'id': row['id'], 'name': row['name']})

    return tags_by_paper_id


def row_to_paper(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    tags_by_paper_id: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if tags_by_paper_id is None:
        tags_by_paper_id = build_tags_by_paper_id(conn, [row['id']])

    return {
        'id': row['id'],
        'arxiv_id': row['arxiv_id'],
        'title': row['title'],
        'abstract': row['abstract'],
        'authors': json.loads(row['authors_json']),
        'categories': json.loads(row['categories_json']),
        'published_at': row['published_at'],
        'arxiv_url': row['arxiv_url'],
        'status': row['status'],
        'project_id': row['project_id'],
        'project_name': row['project_name'],
        'rating': row['rating'],
        'starred': bool(row['starred']),
        'notes': row['notes'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'sort_order': row['sort_order'],
        'tags': tags_by_paper_id.get(row['id'], []),
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


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/projects")
def get_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM projects ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]



@app.get('/api/projects/summary')
def get_projects_summary(include_done: bool = True) -> dict[str, Any]:
    with get_conn() as conn:
        include_done_int = 1 if include_done else 0
        project_rows = conn.execute(
            '''
            SELECT
                pr.id,
                pr.name,
                COUNT(p.id) AS total,
                SUM(CASE WHEN p.status = 'queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN p.status = 'reading' THEN 1 ELSE 0 END) AS reading,
                SUM(CASE WHEN p.status = 'done' THEN 1 ELSE 0 END) AS done
            FROM projects pr
            LEFT JOIN papers p ON p.project_id = pr.id AND (? = 1 OR p.status != 'done')
            GROUP BY pr.id, pr.name
            ORDER BY pr.name COLLATE NOCASE
            ''',
            (include_done_int,),
        ).fetchall()

        unassigned = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM papers
            WHERE project_id IS NULL AND (? = 1 OR status != 'done')
            ''',
            (include_done_int,),
        ).fetchone()['count']

    return {
        'projects': [dict(row) for row in project_rows],
        'unassigned_count': unassigned,
    }

@app.post("/api/projects")
def create_project(payload: ProjectIn) -> dict[str, Any]:
    with get_conn() as conn:
        try:
            with conn:
                cursor = conn.execute("INSERT INTO projects (name) VALUES (?)", (payload.name.strip(),))
            row = conn.execute("SELECT id, name, created_at FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Project already exists")


@app.get("/api/tags")
def get_tags() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/tags")
def create_tag(payload: TagIn) -> dict[str, Any]:
    with get_conn() as conn:
        try:
            with conn:
                cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (payload.name.strip(),))
            row = conn.execute("SELECT id, name, created_at FROM tags WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Tag already exists")


@app.post("/api/papers/import-arxiv")
async def import_arxiv(payload: ImportArxivIn) -> dict[str, Any]:
    arxiv_id = normalize_arxiv_id(payload.value)
    meta = await fetch_arxiv(arxiv_id)

    with get_conn() as conn:
        with conn:
            row = upsert_paper(conn, meta)
        paper = row_to_paper(conn, row)
    return paper


@app.post("/api/papers/import-source")
async def import_source(payload: ImportSourceIn) -> dict[str, Any]:
    meta = await fetch_by_input(payload.value)

    with get_conn() as conn:
        with conn:
            row = upsert_paper(conn, meta)
        paper = row_to_paper(conn, row)
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

    with get_conn() as conn:
        rows: list[sqlite3.Row] = []
        with conn:
            for meta in metas:
                rows.append(upsert_paper(conn, meta))

        tags_by_paper_id = build_tags_by_paper_id(conn, [row["id"] for row in rows])
        papers = [row_to_paper(conn, row, tags_by_paper_id) for row in rows]
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

    with get_conn() as conn:
        rows: list[sqlite3.Row] = []
        with conn:
            for meta in metas:
                rows.append(upsert_paper(conn, meta))

        tags_by_paper_id = build_tags_by_paper_id(conn, [row["id"] for row in rows])
        papers = [row_to_paper(conn, row, tags_by_paper_id) for row in rows]
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

    with get_conn() as conn:
        existing_rows = conn.execute("SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL").fetchall()
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
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    with get_conn() as conn:
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
            escaped = _escape_like(q)
            like = f"%{escaped}%"
            tag_like = f"%{_escape_like(q.lstrip('#'))}%"
            clauses.append(
                "("
                "p.title LIKE ? ESCAPE '\\' OR p.abstract LIKE ? ESCAPE '\\' OR p.authors_json LIKE ? ESCAPE '\\' OR "
                "EXISTS (SELECT 1 FROM paper_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.paper_id = p.id AND (t.name LIKE ? ESCAPE '\\' OR t.name LIKE ? ESCAPE '\\'))"
                ")"
            )
            params.extend([like, like, like, like, tag_like])
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
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        tags_by_paper_id = build_tags_by_paper_id(conn, [row["id"] for row in rows])
        data = [row_to_paper(conn, row, tags_by_paper_id) for row in rows]
    return data


@app.post("/api/papers/reorder")
def reorder_papers(payload: PaperReorderIn) -> dict[str, bool]:
    paper_ids = payload.paper_ids
    if len(set(paper_ids)) != len(paper_ids):
        raise HTTPException(status_code=400, detail="paper_ids must be unique")

    placeholders = ",".join(["?"] * len(paper_ids))

    with get_conn() as conn:
        rows = conn.execute(f"SELECT id, sort_order FROM papers WHERE id IN ({placeholders})", paper_ids).fetchall()
        if len(rows) != len(paper_ids):
            raise HTTPException(status_code=404, detail="One or more papers were not found")

        min_sort_order = min((row["sort_order"] for row in rows if row["sort_order"] is not None), default=1)

        with conn:
            for index, paper_id in enumerate(paper_ids):
                conn.execute(
                    "UPDATE papers SET sort_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (min_sort_order + index, paper_id),
                )
    return {"ok": True}


@app.patch("/api/papers/{paper_id}")
def patch_paper(paper_id: int, payload: PaperPatchIn) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    if 'status' in updates and updates['status'] not in {'queued', 'reading', 'done'}:
        raise HTTPException(status_code=400, detail='Invalid status')

    with get_conn() as conn:
        row = conn.execute('SELECT id FROM papers WHERE id = ?', (paper_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Paper not found')

        if 'project_id' in updates and updates['project_id'] is not None:
            project_row = conn.execute('SELECT id FROM projects WHERE id = ?', (updates['project_id'],)).fetchone()
            if not project_row:
                raise HTTPException(status_code=400, detail='Invalid project_id')

        if updates:
            sets = []
            params: list[Any] = []
            for key, value in updates.items():
                if key not in PATCH_ALLOWED_FIELDS:
                    raise HTTPException(status_code=400, detail=f'Unknown field: {key}')
                sets.append(f'{key} = ?')
                params.append(int(value) if key == 'starred' else value)
            sets.append('updated_at = CURRENT_TIMESTAMP')
            params.append(paper_id)
            try:
                with conn:
                    conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", params)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=400, detail='Invalid paper update payload') from exc

        full_row = conn.execute(
            '''
            SELECT p.*, pr.name AS project_name
            FROM papers p
            LEFT JOIN projects pr ON pr.id = p.project_id
            WHERE p.id = ?
            ''',
            (paper_id,),
        ).fetchone()
        result = row_to_paper(conn, full_row)
    return result


@app.post('/api/papers/{paper_id}/tags')
def add_paper_tag(paper_id: int, payload: TagIn) -> dict[str, Any]:
    with get_conn() as conn:
        paper = conn.execute('SELECT id FROM papers WHERE id = ?', (paper_id,)).fetchone()
        if not paper:
            raise HTTPException(status_code=404, detail='Paper not found')

        with conn:
            conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (payload.name.strip(),))
            tag = conn.execute('SELECT id, name FROM tags WHERE name = ?', (payload.name.strip(),)).fetchone()
            conn.execute('INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)', (paper_id, tag['id']))

    return {'paper_id': paper_id, 'tag': dict(tag)}


@app.delete('/api/papers/{paper_id}/tags/{tag_id}')
def remove_paper_tag(paper_id: int, tag_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        with conn:
            conn.execute('DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?', (paper_id, tag_id))
    return {'ok': True}





@app.delete('/api/papers/{paper_id}')
def delete_paper(paper_id: int) -> dict[str, bool]:
    with get_conn() as conn:
        row = conn.execute('SELECT id FROM papers WHERE id = ?', (paper_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Paper not found')
        with conn:
            conn.execute('DELETE FROM papers WHERE id = ?', (paper_id,))
    return {'ok': True}


@app.delete('/api/projects/{project_id}')
def delete_project(project_id: int) -> dict[str, bool]:
    with get_conn() as conn:
        row = conn.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Project not found')
        with conn:
            conn.execute('DELETE FROM projects WHERE id = ?', (project_id,))
    return {'ok': True}


@app.get('/api/data/export')
def export_data() -> dict[str, Any]:
    with get_conn() as conn:
        project_rows = conn.execute('SELECT name FROM projects ORDER BY name COLLATE NOCASE').fetchall()
        tag_rows = conn.execute('SELECT name FROM tags ORDER BY name COLLATE NOCASE').fetchall()
        paper_rows = conn.execute(
            '''
            SELECT p.*, pr.name AS project_name
            FROM papers p
            LEFT JOIN projects pr ON pr.id = p.project_id
            ORDER BY COALESCE(p.sort_order, 2147483647) ASC, p.created_at DESC
            '''
        ).fetchall()

        tags_by_paper_id = build_tags_by_paper_id(conn, [row["id"] for row in paper_rows])

        papers_payload: list[dict[str, Any]] = []
        for row in paper_rows:
            paper = row_to_paper(conn, row, tags_by_paper_id)
            papers_payload.append(
                {
                    'arxiv_id': paper['arxiv_id'],
                    'title': paper['title'],
                    'abstract': paper['abstract'],
                    'authors': paper['authors'],
                    'categories': paper['categories'],
                    'published_at': paper['published_at'],
                    'arxiv_url': paper['arxiv_url'],
                    'status': paper['status'],
                    'project': paper['project_name'],
                    'rating': paper['rating'],
                    'starred': paper['starred'],
                    'notes': paper['notes'] or '',
                    'tags': [tag['name'] for tag in paper['tags']],
                    'sort_order': paper['sort_order'],
                }
            )

    return {
        'version': 1,
        'projects': [row['name'] for row in project_rows],
        'tags': [row['name'] for row in tag_rows],
        'papers': papers_payload,
    }


@app.post('/api/data/import')
def import_data(payload: DataImportIn) -> dict[str, Any]:
    with get_conn() as conn:
        imported_count = 0
        with conn:
            for project_name in payload.projects:
                normalized_project = (project_name or '').strip()
                if normalized_project:
                    conn.execute('INSERT OR IGNORE INTO projects (name) VALUES (?)', (normalized_project,))

            for tag_name in payload.tags:
                normalized_tag = (tag_name or '').strip()
                if normalized_tag:
                    conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (normalized_tag,))

            project_rows = conn.execute('SELECT id, name FROM projects').fetchall()
            project_ids_by_name = {row['name']: row['id'] for row in project_rows}

            for paper in payload.papers:
                source_id = normalize_source_id(paper.arxiv_id)
                project_name = (paper.project or '').strip()
                project_id = project_ids_by_name.get(project_name) if project_name else None

                meta = {
                    'arxiv_id': source_id,
                    'title': paper.title.strip(),
                    'abstract': paper.abstract or '',
                    'authors': [a for a in paper.authors if a],
                    'categories': [c for c in paper.categories if c],
                    'published_at': paper.published_at,
                    'arxiv_url': paper.arxiv_url,
                    'sort_order': paper.sort_order,
                }
                row = upsert_paper(conn, meta)

                conn.execute(
                    '''
                    UPDATE papers
                    SET status = ?, project_id = ?, rating = ?, starred = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (paper.status, project_id, paper.rating, int(paper.starred), paper.notes, row['id']),
                )

                for tag_name in paper.tags:
                    normalized_tag = (tag_name or '').strip()
                    if not normalized_tag:
                        continue
                    conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (normalized_tag,))
                    tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (normalized_tag,)).fetchone()
                    conn.execute('INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)', (row['id'], tag_row['id']))

                imported_count += 1

        total_papers = conn.execute('SELECT COUNT(*) AS count FROM papers').fetchone()['count']

    return {'ok': True, 'imported_papers': imported_count, 'total_papers': total_papers}


@app.get('/')
def root() -> FileResponse:
    return FileResponse(WEB_DIR / 'index.html')
