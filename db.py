"""
SQLite database layer for bookshelf.

Provides schema definition, connection factory, and migration system.
Uses stdlib sqlite3 only — no ORM, no additional dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable


# ── Schema (migration 1) ─────────────────────────────────────────────────────

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goodreads_id TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    isbn13 TEXT,
    my_rating INTEGER DEFAULT 0 CHECK (my_rating BETWEEN 0 AND 5),
    avg_rating REAL,
    pages INTEGER,
    date_read TEXT,
    date_added TEXT NOT NULL,
    shelves TEXT,
    exclusive_shelf TEXT NOT NULL DEFAULT 'to_read'
        CHECK (exclusive_shelf IN ('read', 'currently_reading', 'to_read')),
    review TEXT,
    notes TEXT,
    cover_url TEXT,
    google_books_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_books_goodreads_id
    ON books(goodreads_id) WHERE goodreads_id IS NOT NULL AND goodreads_id != '';

CREATE INDEX IF NOT EXISTS idx_books_exclusive_shelf ON books(exclusive_shelf);
CREATE INDEX IF NOT EXISTS idx_books_date_read ON books(date_read);
CREATE INDEX IF NOT EXISTS idx_books_title_author
    ON books(title COLLATE NOCASE, author COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_used TEXT
);
"""


def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V1)


_NOTES_SCHEMA = """
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


_ACTIVITY_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('book_added_to_to_read', 'started_reading', 'finished_reading', 'note_added')),
    book_id INTEGER NOT NULL,
    note_id INTEGER,
    book_title TEXT NOT NULL,
    book_author TEXT NOT NULL,
    note_type TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_log_created_at
    ON activity_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_book_id
    ON activity_log(book_id);
"""


_BOOK_SUGGESTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_title TEXT NOT NULL,
    book_author TEXT,
    why TEXT NOT NULL,
    visitor_name TEXT,
    visitor_email TEXT,
    email_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (email_status IN ('pending', 'sent', 'failed')),
    email_sent_at TEXT,
    email_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_book_suggestions_created_at
    ON book_suggestions(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_book_suggestions_email_status
    ON book_suggestions(email_status);
"""


def _migration_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(_NOTES_SCHEMA)
    conn.executescript(_ACTIVITY_LOG_SCHEMA)


def _migration_v3(conn: sqlite3.Connection) -> None:
    note_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(notes)").fetchall()
    }
    if "connected_label" not in note_columns:
        conn.execute("ALTER TABLE notes ADD COLUMN connected_label TEXT")


def _migration_v4(conn: sqlite3.Connection) -> None:
    note_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(notes)").fetchall()
    }
    if "connected_url" not in note_columns:
        conn.execute("ALTER TABLE notes ADD COLUMN connected_url TEXT")


def _migration_v5(conn: sqlite3.Connection) -> None:
    conn.executescript(_BOOK_SUGGESTIONS_SCHEMA)


# ── Migration registry ────────────────────────────────────────────────────────

MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
]


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Migration runner ──────────────────────────────────────────────────────────

def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)


def get_schema_version(conn: sqlite3.Connection) -> int:
    _ensure_schema_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def run_migrations(conn: sqlite3.Connection) -> int:
    _ensure_schema_version_table(conn)
    current = get_schema_version(conn)
    applied = 0

    for version, migrate_fn in MIGRATIONS:
        if version > current:
            migrate_fn(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            conn.commit()
            applied += 1

    return applied


# ── LLM cache helpers ─────────────────────────────────────────────────────────

def get_llm_cache_value(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT value FROM llm_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])


def set_llm_cache_value(conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO llm_cache (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


# ── Book helpers ──────────────────────────────────────────────────────────────

def _row_to_book_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to the dict format the API returns."""
    d = dict(row)
    # Parse shelves from JSON string back to list
    shelves_raw = d.pop("shelves", None)
    d["shelves"] = json.loads(shelves_raw) if shelves_raw else []
    # API returns my_review, not review, for frontend compat
    d["my_review"] = d.pop("review", None)
    return d


def insert_book(conn: sqlite3.Connection, book: dict[str, Any]) -> int:
    shelves = json.dumps(book.get("shelves") or [], ensure_ascii=False)
    cursor = conn.execute(
        """INSERT INTO books
           (goodreads_id, title, author, isbn13, my_rating, avg_rating,
            pages, date_read, date_added, shelves, exclusive_shelf,
            review, notes, cover_url, google_books_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            book.get("goodreads_id") or None,
            book["title"],
            book["author"],
            book.get("isbn13") or None,
            book.get("my_rating", 0),
            book.get("avg_rating"),
            book.get("pages"),
            book.get("date_read") or None,
            book.get("date_added") or "",
            shelves,
            book.get("exclusive_shelf", "to_read"),
            book.get("review") or book.get("my_review") or None,
            book.get("notes") or None,
            book.get("cover_url") or None,
            book.get("google_books_id") or None,
        ),
    )
    return cursor.lastrowid


def get_books_by_shelf(conn: sqlite3.Connection, shelf: str) -> list[dict[str, Any]]:
    if shelf == "read":
        order = "date_read DESC, date_added DESC"
    elif shelf == "currently_reading":
        order = "date_added DESC, date_read DESC"
    else:
        order = "date_added DESC"

    rows = conn.execute(
        f"SELECT * FROM books WHERE exclusive_shelf = ? ORDER BY {order}",
        (shelf,),
    ).fetchall()
    return [_row_to_book_dict(row) for row in rows]


def get_book_by_id(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        return None
    return _row_to_book_dict(row)


def update_book(conn: sqlite3.Connection, book_id: int, fields: dict[str, Any]) -> bool:
    """Update specific fields on a book. Returns True if the row was found."""
    existing = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if existing is None:
        return False

    # Map API field names to DB column names
    if "my_review" in fields and "review" not in fields:
        fields["review"] = fields.pop("my_review")

    if "shelves" in fields:
        fields["shelves"] = json.dumps(fields["shelves"], ensure_ascii=False)

    allowed = {
        "title", "author", "isbn13", "my_rating", "avg_rating", "pages",
        "date_read", "date_added", "shelves", "exclusive_shelf", "review",
        "notes", "cover_url", "google_books_id", "goodreads_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return True

    updates["updated_at"] = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    set_parts = []
    values: list[Any] = []
    for col, val in updates.items():
        if col == "updated_at":
            set_parts.append(f"{col} = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        else:
            set_parts.append(f"{col} = ?")
            values.append(val)

    values.append(book_id)
    conn.execute(
        f"UPDATE books SET {', '.join(set_parts)} WHERE id = ?",
        values,
    )
    return True


def delete_book(conn: sqlite3.Connection, book_id: int) -> bool:
    """Delete a book by ID. Returns True if the row existed."""
    cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return cursor.rowcount > 0


def insert_activity(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    book_id: int,
    book_title: str,
    book_author: str,
    note_id: int | None = None,
    note_type: str | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO activity_log
           (event_type, book_id, note_id, book_title, book_author, note_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_type, book_id, note_id, book_title, book_author, note_type),
    )
    return cursor.lastrowid


def list_activity_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT
               activity_log.id,
               activity_log.event_type,
               activity_log.book_id,
               activity_log.note_id,
               activity_log.book_title,
               activity_log.book_author,
               activity_log.note_type,
               activity_log.created_at,
               CASE WHEN books.id IS NULL THEN 0 ELSE 1 END AS book_exists
           FROM activity_log
           LEFT JOIN books ON books.id = activity_log.book_id
           ORDER BY activity_log.created_at DESC, activity_log.id DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_book_suggestion(
    conn: sqlite3.Connection,
    *,
    book_title: str,
    why: str,
    book_author: str | None = None,
    visitor_name: str | None = None,
    visitor_email: str | None = None,
    email_status: str = "pending",
    email_sent_at: str | None = None,
    email_error: str | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO book_suggestions
           (book_title, book_author, why, visitor_name, visitor_email,
            email_status, email_sent_at, email_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            book_title,
            book_author,
            why,
            visitor_name,
            visitor_email,
            email_status,
            email_sent_at,
            email_error,
        ),
    )
    return cursor.lastrowid


def get_book_suggestion_by_id(
    conn: sqlite3.Connection, suggestion_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM book_suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def update_book_suggestion_email_state(
    conn: sqlite3.Connection,
    suggestion_id: int,
    *,
    email_status: str,
    email_sent_at: str | None = None,
    email_error: str | None = None,
) -> bool:
    cursor = conn.execute(
        """UPDATE book_suggestions
           SET email_status = ?, email_sent_at = ?, email_error = ?
           WHERE id = ?""",
        (email_status, email_sent_at, email_error, suggestion_id),
    )
    return cursor.rowcount > 0
