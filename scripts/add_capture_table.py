"""
Migration script: add the `capture_events` table to the bookshelf database.

Idempotent — safe to run multiple times.
"""

import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "data/bookshelf.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT NOT NULL,
    source_channel TEXT NOT NULL DEFAULT 'telegram',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'discarded')),
    resolved_book_id INTEGER,
    resolved_note_type TEXT,
    resolved_content TEXT,
    resolved_page_or_location TEXT,
    resolved_tags TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_status ON capture_events(status);
CREATE INDEX IF NOT EXISTS idx_capture_created ON capture_events(created_at);
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
            "SELECT name FROM sqlite_master WHERE type='table' AND name='capture_events'"
        ).fetchone()

        if row:
            print("capture_events table already exists")
        else:
            conn.executescript(SCHEMA)
            print("capture_events table created")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
