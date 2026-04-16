"""
SQLite database layer for bookshelf.

Provides schema definition, connection factory, and migration system.
Uses stdlib sqlite3 only — no ORM, no additional dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
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
    client_ip_hash TEXT,
    user_agent TEXT,
    content_fingerprint TEXT,
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
CREATE INDEX IF NOT EXISTS idx_book_suggestions_client_ip_created_at
    ON book_suggestions(client_ip_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_book_suggestions_content_fingerprint_created_at
    ON book_suggestions(content_fingerprint, created_at DESC);
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


def _migration_v6(conn: sqlite3.Connection) -> None:
    suggestion_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(book_suggestions)").fetchall()
    }
    if "client_ip_hash" not in suggestion_columns:
        conn.execute("ALTER TABLE book_suggestions ADD COLUMN client_ip_hash TEXT")
    if "user_agent" not in suggestion_columns:
        conn.execute("ALTER TABLE book_suggestions ADD COLUMN user_agent TEXT")
    if "content_fingerprint" not in suggestion_columns:
        conn.execute("ALTER TABLE book_suggestions ADD COLUMN content_fingerprint TEXT")

    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_book_suggestions_client_ip_created_at
           ON book_suggestions(client_ip_hash, created_at DESC)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_book_suggestions_content_fingerprint_created_at
           ON book_suggestions(content_fingerprint, created_at DESC)"""
    )


# ── Migration registry ────────────────────────────────────────────────────────

def _migration_v7(conn: sqlite3.Connection) -> None:
    book_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(books)").fetchall()
    }
    if "notes" in book_columns:
        conn.execute("ALTER TABLE books DROP COLUMN notes")


_CAPTURE_EVENTS_SCHEMA = """
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


def _migration_v8(conn: sqlite3.Connection) -> None:
    conn.executescript(_CAPTURE_EVENTS_SCHEMA)


_BOOK_READ_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_read_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    started_on TEXT,
    finished_on TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
    CHECK (started_on IS NULL OR started_on = '' OR started_on <= finished_on)
);

CREATE INDEX IF NOT EXISTS idx_book_read_events_book_finished
    ON book_read_events(book_id, finished_on DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_book_read_events_finished
    ON book_read_events(finished_on DESC);
"""


def _migration_v9(conn: sqlite3.Connection) -> None:
    conn.executescript(_BOOK_READ_EVENTS_SCHEMA)
    conn.execute(
        """INSERT INTO book_read_events (book_id, finished_on)
           SELECT books.id, books.date_read
           FROM books
           WHERE books.date_read IS NOT NULL
             AND books.date_read != ''
             AND NOT EXISTS (
                 SELECT 1
                 FROM book_read_events
                 WHERE book_read_events.book_id = books.id
                   AND book_read_events.finished_on = books.date_read
             )"""
    )
    conn.execute(
        """UPDATE books
           SET date_read = (
               SELECT MAX(finished_on)
               FROM book_read_events
               WHERE book_read_events.book_id = books.id
           )
           WHERE EXISTS (
               SELECT 1
               FROM book_read_events
               WHERE book_read_events.book_id = books.id
           )"""
    )


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
    (6, _migration_v6),
    (7, _migration_v7),
    (8, _migration_v8),
    (9, _migration_v9),
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
    d["my_review"] = d.pop("review", None) or ""
    d["isbn13"] = d.get("isbn13") or ""
    d["date_read"] = d.get("date_read") or ""
    d["date_added"] = d.get("date_added") or ""
    d["goodreads_id"] = d.get("goodreads_id") or ""
    d["my_rating"] = d.get("my_rating") or 0
    return d


def _normalize_date_value(value: Any, field_name: str, *, required: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a YYYY-MM-DD date.") from exc
    return raw


def normalize_read_events(raw_events: Any) -> list[dict[str, str | None]]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list):
        raise ValueError("read_events must be a list.")

    normalized: list[dict[str, str | None]] = []
    for idx, raw_event in enumerate(raw_events, start=1):
        if not isinstance(raw_event, dict):
            raise ValueError(f"read_events[{idx}] must be an object.")
        started_on = _normalize_date_value(raw_event.get("started_on"), f"read_events[{idx}].started_on")
        finished_on = _normalize_date_value(
            raw_event.get("finished_on"),
            f"read_events[{idx}].finished_on",
            required=True,
        )
        if started_on and finished_on and started_on > finished_on:
            raise ValueError(f"read_events[{idx}].started_on cannot be after finished_on.")
        normalized.append({"started_on": started_on, "finished_on": finished_on})

    return normalized


def list_read_events(conn: sqlite3.Connection, book_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, book_id, started_on, finished_on, created_at, updated_at
           FROM book_read_events
           WHERE book_id = ?
           ORDER BY finished_on DESC, id DESC""",
        (book_id,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        event["started_on"] = event.get("started_on") or ""
        event["finished_on"] = event.get("finished_on") or ""
        events.append(event)
    return events


def sync_book_latest_read_date(conn: sqlite3.Connection, book_id: int) -> None:
    row = conn.execute(
        "SELECT MAX(finished_on) AS latest FROM book_read_events WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    latest = row["latest"] if row else None
    conn.execute(
        """UPDATE books
           SET date_read = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
           WHERE id = ?""",
        (latest, book_id),
    )


def replace_read_events(conn: sqlite3.Connection, book_id: int, raw_events: Any) -> None:
    events = normalize_read_events(raw_events)
    conn.execute("DELETE FROM book_read_events WHERE book_id = ?", (book_id,))
    for event in events:
        conn.execute(
            """INSERT INTO book_read_events (book_id, started_on, finished_on)
               VALUES (?, ?, ?)""",
            (book_id, event["started_on"], event["finished_on"]),
        )
    sync_book_latest_read_date(conn, book_id)


def insert_book(conn: sqlite3.Connection, book: dict[str, Any]) -> int:
    shelves = json.dumps(book.get("shelves") or [], ensure_ascii=False)
    cursor = conn.execute(
        """INSERT INTO books
           (goodreads_id, title, author, isbn13, my_rating, avg_rating,
            pages, date_read, date_added, shelves, exclusive_shelf,
            review, cover_url, google_books_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            book.get("cover_url") or None,
            book.get("google_books_id") or None,
        ),
    )
    book_id = cursor.lastrowid
    if "read_events" in book:
        replace_read_events(conn, book_id, book.get("read_events") or [])
    elif book.get("date_read"):
        replace_read_events(
            conn,
            book_id,
            [{"started_on": None, "finished_on": book.get("date_read")}],
        )
    return book_id


def get_books_by_shelf(conn: sqlite3.Connection, shelf: str) -> list[dict[str, Any]]:
    if shelf == "read":
        order = """
            CASE WHEN date_read IS NOT NULL AND date_read != '' THEN 0 ELSE 1 END,
            date_read DESC, date_added DESC
        """
    elif shelf == "currently_reading":
        order = "date_added DESC, date_read DESC"
    else:
        order = "date_added DESC"

    rows = conn.execute(
        f"SELECT * FROM books WHERE exclusive_shelf = ? ORDER BY {order}",
        (shelf,),
    ).fetchall()
    books = [_row_to_book_dict(row) for row in rows]
    for book in books:
        book["read_events"] = list_read_events(conn, book["id"])
        if not book["read_events"] and book.get("date_read"):
            book["read_events"] = [{"id": None, "started_on": "", "finished_on": book["date_read"]}]
    return books


def get_book_by_id(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        return None
    book = _row_to_book_dict(row)
    book["read_events"] = list_read_events(conn, book_id)
    if not book["read_events"] and book.get("date_read"):
        book["read_events"] = [{"id": None, "started_on": "", "finished_on": book["date_read"]}]
    return book


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
        "cover_url", "google_books_id", "goodreads_id",
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
               CASE WHEN books.id IS NULL THEN 0 ELSE 1 END AS book_exists,
               CASE WHEN notes.id IS NULL THEN 0 ELSE 1 END AS note_exists
           FROM activity_log
           LEFT JOIN books ON books.id = activity_log.book_id
           LEFT JOIN notes ON notes.id = activity_log.note_id
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
    client_ip_hash: str | None = None,
    user_agent: str | None = None,
    content_fingerprint: str | None = None,
    email_status: str = "pending",
    email_sent_at: str | None = None,
    email_error: str | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO book_suggestions
           (book_title, book_author, why, visitor_name, visitor_email,
            client_ip_hash, user_agent, content_fingerprint,
            email_status, email_sent_at, email_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            book_title,
            book_author,
            why,
            visitor_name,
            visitor_email,
            client_ip_hash,
            user_agent,
            content_fingerprint,
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


def count_recent_book_suggestions(
    conn: sqlite3.Connection,
    *,
    client_ip_hash: str,
    since_iso: str,
) -> int:
    row = conn.execute(
        """SELECT COUNT(*)
           FROM book_suggestions
           WHERE client_ip_hash = ?
             AND created_at >= ?""",
        (client_ip_hash, since_iso),
    ).fetchone()
    return int(row[0] or 0)


def count_book_suggestions_since(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
) -> int:
    row = conn.execute(
        """SELECT COUNT(*)
           FROM book_suggestions
           WHERE created_at >= ?""",
        (since_iso,),
    ).fetchone()
    return int(row[0] or 0)


def count_sent_book_suggestion_emails_since(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
) -> int:
    row = conn.execute(
        """SELECT COUNT(*)
           FROM book_suggestions
           WHERE email_status = 'sent'
             AND COALESCE(email_sent_at, created_at) >= ?""",
        (since_iso,),
    ).fetchone()
    return int(row[0] or 0)


def find_recent_duplicate_book_suggestion(
    conn: sqlite3.Connection,
    *,
    client_ip_hash: str,
    content_fingerprint: str,
    since_iso: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT *
           FROM book_suggestions
           WHERE client_ip_hash = ?
             AND content_fingerprint = ?
             AND created_at >= ?
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (client_ip_hash, content_fingerprint, since_iso),
    ).fetchone()
    if row is None:
        return None
    return dict(row)
