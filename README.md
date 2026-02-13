# vibe-paper-stack

A lightweight local web app to track arXiv papers in a reading queue.

## Features
- Import a paper from an arXiv ID/URL, LessWrong URL, or direct PDF URL
- For direct PDF URLs, attempts first-page title/abstract extraction with fallback metadata
- Find papers by author from arXiv and batch-add selected results
- Queue workflow: `queued`, `reading`, `done`
- Project grouping and tagging
- Search by title/abstract/authors
- Hide completed papers by default
- Star and 1-5 rating support
- Notes per paper

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## API highlights
- `POST /api/papers/import-arxiv`
- `POST /api/papers/import-arxiv-batch`
- `GET /api/arxiv/search-by-author`
- `GET /api/papers` (supports filters)
- `PATCH /api/papers/{id}`
- `POST /api/papers/{id}/tags`
- `DELETE /api/papers/{id}/tags/{tag_id}`
- `GET/POST /api/projects`
- `GET /api/projects/summary`
- `GET/POST /api/tags`
- `GET /api/data/export`
- `POST /api/data/import`
