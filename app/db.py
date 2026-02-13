from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "papers.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arxiv_id TEXT UNIQUE,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL DEFAULT '',
                authors_json TEXT NOT NULL DEFAULT '[]',
                categories_json TEXT NOT NULL DEFAULT '[]',
                published_at TEXT,
                arxiv_url TEXT,
                status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'reading', 'done')),
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                rating INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
                starred INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS paper_tags (
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (paper_id, tag_id)
            );

            CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
            CREATE INDEX IF NOT EXISTS idx_papers_project_id ON papers(project_id);
            CREATE INDEX IF NOT EXISTS idx_papers_starred ON papers(starred);
            CREATE INDEX IF NOT EXISTS idx_papers_rating ON papers(rating);

            INSERT OR IGNORE INTO projects (name) VALUES ('Main');
            """
        )
    conn.close()
