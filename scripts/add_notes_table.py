"""
Migration script: add the `notes` table to the bookshelf database.

Idempotent — safe to run multiple times.
"""

import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "data/bookshelf.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL DEFAULT 'book'
        CHECK (source_type IN ('book', 'article', 'movie', 'blog', 'report', 'other')),
    source_id INTEGER NOT NULL,
    note_type TEXT NOT NULL DEFAULT 'thought'
        CHECK (note_type IN ('thought', 'quote', 'connection', 'disagreement', 'question')),
    content TEXT NOT NULL,
    page_or_location TEXT,
    connected_label TEXT,
    connected_url TEXT,
    connected_source_type TEXT,
    connected_source_id INTEGER,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type);
CREATE INDEX IF NOT EXISTS idx_notes_connected ON notes(connected_source_type, connected_source_id);
"""


def main() -> None:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        # Check if table already exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'"
        ).fetchone()

        if row:
            print("Notes table already exists.")
        else:
            conn.executescript(SCHEMA)
            print("Notes table created.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
