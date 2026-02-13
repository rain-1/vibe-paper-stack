# vibe-paper-stack

A lightweight local web app to track arXiv papers in a reading queue.

## Features
- Import a paper from an arXiv ID or URL (fetches title, abstract, authors, categories)
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
- `GET /api/papers` (supports filters)
- `PATCH /api/papers/{id}`
- `POST /api/papers/{id}/tags`
- `DELETE /api/papers/{id}/tags/{tag_id}`
- `GET/POST /api/projects`
- `GET/POST /api/tags`
