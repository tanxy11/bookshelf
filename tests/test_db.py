"""Tests for db.py and the JSON-to-SQLite migration."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as db_module
from bookshelf_data import (
    BookshelfDB,
    BookshelfStore,
    compute_books_hash,
    default_books_payload,
    default_llm_cache,
)
from db import (
    delete_book,
    get_book_by_id,
    get_book_suggestion_by_id,
    get_connection,
    get_llm_cache_value,
    get_schema_version,
    insert_book,
    insert_book_suggestion,
    list_activity_rows,
    replace_read_events,
    run_migrations,
    set_llm_cache_value,
    update_book,
)

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None


# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_BOOKS_PAYLOAD = {
    "generated_at": "2026-03-22T12:00:00Z",
    "books": {
        "read": [
            {
                "title": "Dune",
                "author": "Frank Herbert",
                "isbn13": "9780441013593",
                "my_rating": 5,
                "avg_rating": 4.25,
                "pages": 688,
                "date_read": "2026-01-15",
                "date_added": "2025-12-01",
                "shelves": ["sci-fi", "favorites"],
                "exclusive_shelf": "read",
                "my_review": "A masterpiece of world-building.",
                "goodreads_id": "234225",
            },
            {
                "title": "1984",
                "author": "George Orwell",
                "isbn13": "",
                "my_rating": 4,
                "avg_rating": 4.19,
                "pages": 328,
                "date_read": "2025-11-20",
                "date_added": "2025-10-01",
                "shelves": ["dystopia", "classics"],
                "exclusive_shelf": "read",
                "my_review": None,
                "goodreads_id": "40961427",
            },
            {
                "title": "No Date Book",
                "author": "Unknown Author",
                "isbn13": "",
                "my_rating": 3,
                "avg_rating": 3.5,
                "pages": 200,
                "date_read": "",
                "date_added": "2024-01-01",
                "shelves": [],
                "exclusive_shelf": "read",
                "my_review": "",
                "goodreads_id": "",
            },
        ],
        "currently_reading": [
            {
                "title": "Sapiens",
                "author": "Yuval Noah Harari",
                "isbn13": "9780062316110",
                "my_rating": 0,
                "avg_rating": 4.39,
                "pages": 443,
                "date_read": "",
                "date_added": "2026-03-01",
                "shelves": ["history"],
                "exclusive_shelf": "currently_reading",
                "my_review": None,
                "goodreads_id": "23692271",
            },
        ],
        "to_read": [
            {
                "title": "Project Hail Mary",
                "author": "Andy Weir",
                "isbn13": "9780593135204",
                "my_rating": 0,
                "avg_rating": 4.52,
                "pages": 476,
                "date_read": "",
                "date_added": "2026-02-15",
                "shelves": ["sci-fi"],
                "exclusive_shelf": "to_read",
                "my_review": None,
                "goodreads_id": "54493401",
            },
        ],
    },
    "stats": {
        "total_read": 3,
        "total_to_read": 1,
        "currently_reading_count": 1,
        "avg_my_rating": 4.0,
        "books_this_year": 1,
        "top_authors": [
            {"author": "Frank Herbert", "count": 1},
            {"author": "George Orwell", "count": 1},
            {"author": "Unknown Author", "count": 1},
        ],
    },
}

SAMPLE_LLM_CACHE = {
    "books_hash": "abc123",
    "generated_at": "2026-03-22T13:00:00Z",
    "dry_run": False,
    "taste_profile": {
        "summary": "A reader of depth.",
        "traits": [{"label": "Complexity", "explanation": "Prefers dense reads."}],
        "blind_spots": "Light romance is absent.",
    },
    "recommendations": {
        "opus": {
            "model": "claude-test",
            "books": [
                {
                    "title": "Foundation",
                    "author": "Isaac Asimov",
                    "reason": "Classic sci-fi.",
                    "confidence": "high",
                }
            ],
            "reasoning": "Sci-fi lover strategy.",
        },
        "gpt45": {"model": "gpt-test", "error": "unavailable"},
        "gemini": {
            "model": "gemini-test",
            "books": [
                {
                    "title": "The Left Hand of Darkness",
                    "author": "Ursula K. Le Guin",
                    "reason": "Thoughtful speculative fiction.",
                    "confidence": "medium",
                }
            ],
            "reasoning": "Leans toward reflective science fiction.",
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

TEST_AUTH_TOKEN = "test-secret-token"
TEST_AUTH_HEADER = {"Authorization": f"Bearer {TEST_AUTH_TOKEN}"}


def _make_test_db(tmp_dir: str) -> Path:
    """Create a SQLite DB with sample data and return its path."""
    import hashlib

    db_path = Path(tmp_dir) / "test.db"
    conn = get_connection(db_path)
    run_migrations(conn)

    for shelf_key in ("read", "currently_reading", "to_read"):
        for book in SAMPLE_BOOKS_PAYLOAD["books"].get(shelf_key, []):
            insert_book(conn, {
                "goodreads_id": book.get("goodreads_id") or None,
                "title": book["title"],
                "author": book["author"],
                "isbn13": book.get("isbn13") or None,
                "my_rating": book.get("my_rating", 0),
                "avg_rating": book.get("avg_rating"),
                "pages": book.get("pages"),
                "date_read": book.get("date_read") or None,
                "date_added": book.get("date_added") or "",
                "shelves": book.get("shelves", []),
                "exclusive_shelf": shelf_key,
                "review": book.get("my_review") or None,
            })

    # Insert LLM cache
    set_llm_cache_value(conn, "metadata", {
        "books_hash": SAMPLE_LLM_CACHE["books_hash"],
        "generated_at": SAMPLE_LLM_CACHE["generated_at"],
        "dry_run": SAMPLE_LLM_CACHE["dry_run"],
    })
    set_llm_cache_value(conn, "taste_profile", SAMPLE_LLM_CACHE["taste_profile"])
    set_llm_cache_value(conn, "recommendations", SAMPLE_LLM_CACHE["recommendations"])

    # Insert auth token
    token_hash = hashlib.sha256(TEST_AUTH_TOKEN.encode()).hexdigest()
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, label) VALUES (?, ?)",
        (token_hash, "test"),
    )

    conn.commit()
    conn.close()
    return db_path


def _make_test_json(tmp_dir: str) -> tuple[Path, Path]:
    """Create JSON files with sample data and return their paths."""
    books_path = Path(tmp_dir) / "books.json"
    llm_path = Path(tmp_dir) / "llm_cache.json"
    books_path.write_text(json.dumps(SAMPLE_BOOKS_PAYLOAD), encoding="utf-8")
    llm_path.write_text(json.dumps(SAMPLE_LLM_CACHE), encoding="utf-8")
    return books_path, llm_path


# ── Tests: db.py ──────────────────────────────────────────────────────────────

class DbSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_migrations_create_tables(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("books", tables)
        self.assertIn("llm_cache", tables)
        self.assertIn("auth_tokens", tables)
        self.assertIn("notes", tables)
        self.assertIn("activity_log", tables)
        self.assertIn("book_suggestions", tables)
        self.assertIn("capture_events", tables)
        self.assertIn("book_read_events", tables)
        self.assertIn("schema_version", tables)
        read_event_columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(book_read_events)").fetchall()
        }
        self.assertEqual(read_event_columns["finished_on"]["notnull"], 0)
        conn.close()

    def test_schema_version_is_10(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 10)
        conn.close()

    def test_migrations_are_idempotent(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 10)
        conn.close()

    def test_existing_notes_table_upgrades_cleanly(self):
        conn = get_connection(self.db_path)
        db_module._migration_v1(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL DEFAULT 'book'
                    CHECK (source_type IN ('book', 'article', 'movie', 'blog', 'report', 'other')),
                source_id INTEGER NOT NULL,
                note_type TEXT NOT NULL DEFAULT 'thought'
                    CHECK (note_type IN ('thought', 'quote', 'connection', 'disagreement', 'question')),
                content TEXT NOT NULL,
                page_or_location TEXT,
                connected_source_type TEXT,
                connected_source_id INTEGER,
                tags TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_type, source_id);
            CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type);
            CREATE INDEX IF NOT EXISTS idx_notes_connected ON notes(connected_source_type, connected_source_id);
        """)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()

        applied = run_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        note_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(notes)").fetchall()
        }
        suggestion_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(book_suggestions)").fetchall()
        }

        self.assertEqual(applied, 9)
        self.assertEqual(get_schema_version(conn), 10)
        self.assertIn("notes", tables)
        self.assertIn("activity_log", tables)
        self.assertIn("book_suggestions", tables)
        self.assertIn("capture_events", tables)
        self.assertIn("book_read_events", tables)
        self.assertIn("connected_label", note_columns)
        self.assertIn("connected_url", note_columns)
        self.assertIn("client_ip_hash", suggestion_columns)
        self.assertIn("user_agent", suggestion_columns)
        self.assertIn("content_fingerprint", suggestion_columns)
        conn.close()

    def test_read_events_migration_backfills_existing_date_read(self):
        conn = get_connection(self.db_path)
        db_module._migration_v1(conn)
        conn.execute(
            """INSERT INTO books
               (title, author, date_read, date_added, exclusive_shelf)
               VALUES (?, ?, ?, ?, ?)""",
            ("Reread Book", "Author", "2020-05-01", "2020-04-01", "read"),
        )
        conn.execute(
            """INSERT INTO books
               (title, author, date_read, date_added, exclusive_shelf)
               VALUES (?, ?, ?, ?, ?)""",
            ("No Date", "Author", None, "2020-04-01", "read"),
        )
        conn.execute(
            """INSERT INTO books
               (title, author, date_read, date_added, exclusive_shelf)
               VALUES (?, ?, ?, ?, ?)""",
            ("Blank Date", "Author", "", "2020-04-01", "read"),
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()

        run_migrations(conn)
        rows = conn.execute(
            "SELECT book_id, finished_on FROM book_read_events ORDER BY book_id"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finished_on"], "2020-05-01")
        conn.close()

    def test_read_events_finished_on_is_nullable_after_v10_migration(self):
        conn = get_connection(self.db_path)
        db_module._migration_v1(conn)
        book_id = insert_book(conn, {
            "title": "Started Book",
            "author": "Author",
            "date_added": "2026-04-16",
            "exclusive_shelf": "currently_reading",
        })
        db_module._migration_v9(conn)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (9)")
        conn.commit()

        run_migrations(conn)
        columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(book_read_events)").fetchall()
        }
        self.assertEqual(get_schema_version(conn), 10)
        self.assertEqual(columns["finished_on"]["notnull"], 0)

        replace_read_events(conn, book_id, [{"started_on": "2026-04-16"}])
        conn.commit()
        row = conn.execute(
            "SELECT started_on, finished_on FROM book_read_events WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        book = conn.execute("SELECT date_read FROM books WHERE id = ?", (book_id,)).fetchone()
        self.assertEqual(row["started_on"], "2026-04-16")
        self.assertIsNone(row["finished_on"])
        self.assertIsNone(book["date_read"])
        conn.close()

    def test_insert_and_list_activity_rows(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "Test Book",
            "author": "Test Author",
            "date_added": "2026-01-01",
            "exclusive_shelf": "to_read",
        })
        db_module.insert_activity(
            conn,
            event_type="book_added_to_to_read",
            book_id=book_id,
            book_title="Test Book",
            book_author="Test Author",
        )
        conn.commit()

        rows = list_activity_rows(conn, limit=10, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "book_added_to_to_read")
        self.assertEqual(rows[0]["book_title"], "Test Book")
        self.assertEqual(rows[0]["book_exists"], 1)
        conn.close()

    def test_insert_and_retrieve_book(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "Test Book",
            "author": "Test Author",
            "isbn13": "1234567890123",
            "my_rating": 4,
            "avg_rating": 4.0,
            "pages": 300,
            "date_read": "2026-01-01",
            "date_added": "2025-12-01",
            "shelves": ["fiction", "favorites"],
            "exclusive_shelf": "read",
            "review": "Great book.",
        })
        conn.commit()

        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        self.assertEqual(row["title"], "Test Book")
        self.assertEqual(row["my_rating"], 4)
        self.assertEqual(json.loads(row["shelves"]), ["fiction", "favorites"])
        self.assertEqual(row["review"], "Great book.")
        conn.close()

    def test_insert_and_retrieve_book_suggestion(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        suggestion_id = insert_book_suggestion(
            conn,
            book_title="The Magic Mountain",
            book_author="Thomas Mann",
            why="The taste profile made me think of ambitious, reflective novels.",
            visitor_name="A reader",
            visitor_email="reader@example.com",
            client_ip_hash="hash123",
            user_agent="TestAgent/1.0",
            content_fingerprint="fingerprint123",
        )
        conn.commit()

        row = get_book_suggestion_by_id(conn, suggestion_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["book_title"], "The Magic Mountain")
        self.assertEqual(row["email_status"], "pending")
        self.assertEqual(row["visitor_email"], "reader@example.com")
        self.assertEqual(row["client_ip_hash"], "hash123")
        self.assertEqual(row["user_agent"], "TestAgent/1.0")
        self.assertEqual(row["content_fingerprint"], "fingerprint123")
        conn.close()

    def test_llm_cache_round_trip(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        data = {"summary": "Test", "traits": []}
        set_llm_cache_value(conn, "taste_profile", data)
        result = get_llm_cache_value(conn, "taste_profile")
        self.assertEqual(result, data)
        conn.close()

    def test_llm_cache_upsert(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        set_llm_cache_value(conn, "key1", {"v": 1})
        set_llm_cache_value(conn, "key1", {"v": 2})
        result = get_llm_cache_value(conn, "key1")
        self.assertEqual(result["v"], 2)
        conn.close()

    def test_wal_mode_enabled(self):
        conn = get_connection(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        conn.close()

    def test_get_book_by_id(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "Test", "author": "Author",
            "date_added": "2026-01-01", "exclusive_shelf": "read",
        })
        conn.commit()
        book = get_book_by_id(conn, book_id)
        self.assertEqual(book["title"], "Test")
        self.assertIsNone(get_book_by_id(conn, 9999))
        conn.close()

    def test_update_book(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "Old", "author": "Author",
            "date_added": "2026-01-01", "exclusive_shelf": "read",
        })
        conn.commit()
        self.assertTrue(update_book(conn, book_id, {"title": "New"}))
        book = get_book_by_id(conn, book_id)
        self.assertEqual(book["title"], "New")
        self.assertFalse(update_book(conn, 9999, {"title": "X"}))
        conn.close()

    def test_update_book_maps_my_review(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "T", "author": "A",
            "date_added": "2026-01-01", "exclusive_shelf": "read",
        })
        conn.commit()
        update_book(conn, book_id, {"my_review": "Great!"})
        book = get_book_by_id(conn, book_id)
        self.assertEqual(book["my_review"], "Great!")
        conn.close()

    def test_delete_book(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        book_id = insert_book(conn, {
            "title": "Gone", "author": "Author",
            "date_added": "2026-01-01", "exclusive_shelf": "read",
        })
        conn.commit()
        self.assertTrue(delete_book(conn, book_id))
        self.assertFalse(delete_book(conn, book_id))
        self.assertIsNone(get_book_by_id(conn, book_id))
        conn.close()


# ── Tests: BookshelfDB ────────────────────────────────────────────────────────

class BookshelfDBTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = _make_test_db(self.tempdir.name)
        self.db_store = BookshelfDB(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_books_returns_correct_counts(self):
        books = self.db_store.books()
        self.assertEqual(len(books["books"]["read"]), 3)
        self.assertEqual(len(books["books"]["currently_reading"]), 1)
        self.assertEqual(len(books["books"]["to_read"]), 1)

    def test_books_stats_match(self):
        books = self.db_store.books()
        stats = books["stats"]
        self.assertEqual(stats["total_read"], 3)
        self.assertEqual(stats["total_to_read"], 1)
        self.assertEqual(stats["currently_reading_count"], 1)
        self.assertEqual(stats["avg_my_rating"], 4.0)
        self.assertEqual(stats["read_completions"], 2)
        self.assertEqual(stats["read_completions_this_year"], 1)

    def test_books_read_sorted_by_date_read_desc(self):
        books = self.db_store.books()
        read = books["books"]["read"]
        # Books with dates should come first, sorted desc
        self.assertEqual(read[0]["title"], "Dune")  # 2026-01-15
        self.assertEqual(read[1]["title"], "1984")  # 2025-11-20
        self.assertEqual(read[2]["title"], "No Date Book")  # no date → last

    def test_book_fields_match_json_conventions(self):
        books = self.db_store.books()
        book = books["books"]["read"][0]
        # Should have my_review not review
        self.assertIn("my_review", book)
        self.assertNotIn("review", book)
        # id, cover_url, google_books_id are kept for CRUD/frontend
        self.assertIn("id", book)
        self.assertNotIn("notes", book)
        # DB-only timestamps are removed
        self.assertNotIn("created_at", book)
        self.assertNotIn("updated_at", book)
        # Empty strings not None
        self.assertIsInstance(book.get("isbn13"), str)
        self.assertIsInstance(book.get("date_read"), str)
        self.assertIsInstance(book.get("read_events"), list)
        # Shelves should be a list
        self.assertIsInstance(book["shelves"], list)

    def test_read_events_are_returned_and_date_read_is_latest_finish(self):
        conn = get_connection(self.db_path)
        replace_read_events(conn, 2, [
            {"started_on": "2021-01-01", "finished_on": "2021-02-01"},
            {"started_on": "2026-02-01", "finished_on": "2026-03-05"},
        ])
        conn.commit()
        conn.close()

        books = self.db_store.books()
        read = books["books"]["read"]
        self.assertEqual(read[0]["title"], "1984")
        self.assertEqual(read[0]["date_read"], "2026-03-05")
        self.assertEqual([event["finished_on"] for event in read[0]["read_events"]], [
            "2026-03-05",
            "2021-02-01",
        ])

    def test_replace_read_events_allows_start_only_and_validates_start_order(self):
        conn = get_connection(self.db_path)
        replace_read_events(conn, 1, [{"started_on": "2026-01-01"}])
        row = conn.execute(
            "SELECT started_on, finished_on FROM book_read_events WHERE book_id = ?",
            (1,),
        ).fetchone()
        self.assertEqual(row["started_on"], "2026-01-01")
        self.assertIsNone(row["finished_on"])
        with self.assertRaises(ValueError):
            replace_read_events(conn, 1, [{"started_on": "2026-02-01", "finished_on": "2026-01-01"}])
        conn.close()

    def test_book_empty_review_is_empty_string(self):
        books = self.db_store.books()
        no_date_book = books["books"]["read"][2]
        self.assertEqual(no_date_book["my_review"], "")

    def test_taste_profile_returns_correctly(self):
        tp = self.db_store.taste_profile()
        self.assertIsNotNone(tp)
        self.assertEqual(tp["summary"], "A reader of depth.")

    def test_recommendations_returns_correctly(self):
        recs = self.db_store.recommendations()
        self.assertIsNotNone(recs)
        self.assertIn("opus", recs)
        self.assertIn("gemini", recs)
        self.assertEqual(recs["opus"]["books"][0]["title"], "Foundation")

    def test_health_returns_expected_keys(self):
        h = self.db_store.health()
        expected_keys = {
            "status", "generated_at", "books_generated_at",
            "llm_generated_at", "books_hash", "dry_run",
            "has_books", "has_taste_profile", "has_recommendations",
            "llm_input_hash", "current_llm_input_hash",
            "llm_outdated", "llm_targets",
        }
        self.assertEqual(set(h.keys()), expected_keys)
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["has_books"])
        self.assertTrue(h["has_taste_profile"])
        self.assertTrue(h["has_recommendations"])

    def test_llm_cache_reconstructs_full_structure(self):
        cache = self.db_store.llm_cache()
        self.assertIn("books_hash", cache)
        self.assertIn("generated_at", cache)
        self.assertIn("dry_run", cache)
        self.assertIn("taste_profile", cache)
        self.assertIn("recommendations", cache)


# ── Tests: BookshelfDB vs BookshelfStore equivalence ──────────────────────────

class EquivalenceTests(unittest.TestCase):
    """Verify BookshelfDB produces the same output as BookshelfStore."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = _make_test_db(self.tempdir.name)
        self.books_path, self.llm_path = _make_test_json(self.tempdir.name)
        self.json_store = BookshelfStore(self.books_path, self.llm_path)
        self.db_store = BookshelfDB(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_taste_profile_matches(self):
        self.assertEqual(self.json_store.taste_profile(), self.db_store.taste_profile())

    def test_recommendations_matches(self):
        self.assertEqual(
            self.json_store.recommendations(), self.db_store.recommendations()
        )

    def test_books_counts_match(self):
        jb = self.json_store.books()
        db = self.db_store.books()
        for shelf in ("read", "currently_reading", "to_read"):
            self.assertEqual(
                len(jb["books"][shelf]),
                len(db["books"][shelf]),
                f"Count mismatch for shelf {shelf}",
            )

    def test_books_stats_match(self):
        js = self.json_store.books()["stats"]
        ds = self.db_store.books()["stats"]
        for key in ("total_read", "total_to_read", "currently_reading_count", "avg_my_rating"):
            self.assertEqual(js[key], ds[key], f"Stats mismatch for {key}")

    def test_first_read_book_fields_match(self):
        """First read book (has date_read, review, shelves) should match exactly."""
        jb = self.json_store.books()["books"]["read"][0]
        db = self.db_store.books()["books"]["read"][0]
        for key in ("title", "author", "isbn13", "my_rating", "avg_rating",
                     "pages", "date_read", "date_added", "shelves",
                     "exclusive_shelf", "my_review", "goodreads_id"):
            self.assertEqual(
                jb.get(key), db.get(key),
                f"Field mismatch for '{key}': json={jb.get(key)!r} db={db.get(key)!r}"
            )

    def test_health_structure_matches(self):
        jh = self.json_store.health()
        dh = self.db_store.health()
        # Same keys
        self.assertEqual(set(jh.keys()), set(dh.keys()))
        # Same values for non-timestamp fields
        for key in ("status", "dry_run", "has_books", "has_taste_profile",
                     "has_recommendations", "books_hash"):
            self.assertEqual(jh[key], dh[key], f"Health mismatch for {key}")


# ── Tests: API with SQLite backend ────────────────────────────────────────────

@unittest.skipIf(TestClient is None, "fastapi is not installed")
class ApiSqliteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = _make_test_db(self.tempdir.name)

        self.original_env = os.environ.copy()
        os.environ["DB_PATH"] = str(self.db_path)
        # Clear JSON paths so we don't fall back
        os.environ.pop("BOOKS_DATA", None)
        os.environ.pop("LLM_CACHE_DATA", None)
        os.environ["BOOKSHELF_CORS_ORIGINS"] = "https://book.tanxy.net"
        # Never let routine API tests talk to real SMTP. Tests that exercise
        # delivery behavior should opt in explicitly with mocks.
        for key in (
            "BOOK_SUGGESTIONS_TO_EMAIL",
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_FROM_EMAIL",
            "SMTP_USE_STARTTLS",
            "SMTP_USE_SSL",
            "SMTP_TIMEOUT_SECONDS",
        ):
            os.environ.pop(key, None)

        if "api.main" in sys.modules:
            self.api_main = importlib.reload(sys.modules["api.main"])
        else:
            self.api_main = importlib.import_module("api.main")

        self.suggestion_email_config_patcher = patch(
            "api.suggestions.get_suggestion_email_config",
            return_value=None,
        )
        self.suggestion_email_send_patcher = patch(
            "api.suggestions.send_book_suggestion_notification",
        )
        self.mock_get_suggestion_email_config = self.suggestion_email_config_patcher.start()
        self.mock_send_book_suggestion_notification = self.suggestion_email_send_patcher.start()
        self.client = TestClient(self.api_main.app)

    def tearDown(self):
        self.suggestion_email_send_patcher.stop()
        self.suggestion_email_config_patcher.stop()
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tempdir.cleanup()

    def _set_activity_created_at(self, activity_id: int, created_at: str) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE activity_log SET created_at = ? WHERE id = ?",
            (created_at, activity_id),
        )
        conn.commit()
        conn.close()

    def _set_note_activity_created_at(self, note_id: int, created_at: str) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE activity_log SET created_at = ? WHERE event_type = 'note_added' AND note_id = ?",
            (created_at, note_id),
        )
        conn.commit()
        conn.close()

    def test_books_endpoint(self):
        resp = self.client.get("/api/books")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["books"]["read"]), 3)
        self.assertEqual(data["books"]["read"][0]["title"], "Dune")
        self.assertEqual(data["books"]["read"][0]["read_events"][0]["finished_on"], "2026-01-15")

    def test_single_book_endpoint(self):
        resp = self.client.get("/api/books/1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title"], "Dune")
        self.assertEqual(data["author"], "Frank Herbert")
        self.assertEqual(data["note_count"], 0)
        self.assertEqual(data["read_events"][0]["finished_on"], "2026-01-15")

    def test_single_book_endpoint_not_found(self):
        resp = self.client.get("/api/books/99999")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "Book not found.")

    def test_taste_profile_endpoint(self):
        resp = self.client.get("/api/taste-profile")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], "A reader of depth.")

    def test_recommendations_endpoint(self):
        resp = self.client.get("/api/recommendations")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("opus", resp.json())
        self.assertIn("gemini", resp.json())

    def test_health_endpoint(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["has_books"])
        self.assertTrue(data["has_taste_profile"])
        self.assertEqual(data["data_backend"], "sqlite")

    def test_activity_endpoint_empty_initially(self):
        resp = self.client.get("/api/activity")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["pagination"]["has_more"])

    def test_llm_status_endpoint(self):
        resp = self.client.get("/api/llm-status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "idle")

    def test_llm_regenerate_requires_auth(self):
        resp = self.client.post("/api/llm/regenerate")
        self.assertEqual(resp.status_code, 401)

    def test_llm_regenerate_rejects_bad_token(self):
        os.environ["BOOKSHELF_AUTH_TOKEN"] = "correct-token"
        if "api.main" in sys.modules:
            importlib.reload(sys.modules["api.main"])
        client = TestClient(sys.modules["api.main"].app)
        resp = client.post(
            "/api/llm/regenerate",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_book(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "New Book", "author": "New Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["title"], "New Book")
        self.assertEqual(data["author"], "New Author")
        self.assertEqual(data["shelves"], ["to-read"])

    def test_create_book_logs_activity_for_each_initial_shelf(self):
        cases = [
            ("Shelf One", "to_read", "book_added_to_to_read", "Added Shelf One to to-read"),
            ("Shelf Two", "currently_reading", "started_reading", "Started Shelf Two"),
            ("Shelf Three", "read", "finished_reading", "Finished Shelf Three"),
        ]

        for title, shelf, _, _ in cases:
            resp = self.client.post(
                "/api/books",
                json={"title": title, "author": "Author", "exclusive_shelf": shelf},
                headers=TEST_AUTH_HEADER,
            )
            self.assertEqual(resp.status_code, 201)

        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual(len(activity), 3)
        for item, (_, _, event_type, summary) in zip(activity, reversed(cases)):
            self.assertEqual(item["event_type"], event_type)
            self.assertEqual(item["summary"], summary)

    def test_create_book_requires_auth(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "X", "author": "Y"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_book_requires_title_and_author(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Only Title"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.client.get("/api/activity").json()["items"], [])

    def test_update_book(self):
        # Create a book first
        resp = self.client.post(
            "/api/books",
            json={"title": "Old Title", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/books/{book_id}",
            json={"title": "New Title"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "New Title")

    def test_create_book_accepts_read_events_and_sets_latest_date_read(self):
        resp = self.client.post(
            "/api/books",
            json={
                "title": "Reread Book",
                "author": "Author",
                "exclusive_shelf": "read",
                "read_events": [
                    {"started_on": "2020-01-01", "finished_on": "2020-02-01"},
                    {"started_on": "2026-01-01", "finished_on": "2026-02-01"},
                ],
            },
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["date_read"], "2026-02-01")
        self.assertEqual([event["finished_on"] for event in data["read_events"]], [
            "2026-02-01",
            "2020-02-01",
        ])

    def test_create_currently_reading_accepts_start_only_read_event(self):
        resp = self.client.post(
            "/api/books",
            json={
                "title": "Just Started",
                "author": "Author",
                "exclusive_shelf": "currently_reading",
                "read_events": [
                    {"started_on": "2026-04-16"},
                ],
            },
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["date_read"], "")
        self.assertEqual(data["read_events"][0]["started_on"], "2026-04-16")
        self.assertEqual(data["read_events"][0]["finished_on"], "")

    def test_update_book_replaces_read_events_and_resyncs_latest_date(self):
        resp = self.client.post(
            "/api/books",
            json={
                "title": "Eventful Book",
                "author": "Author",
                "exclusive_shelf": "read",
                "date_read": "2020-01-01",
            },
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/books/{book_id}",
            json={
                "read_events": [
                    {"started_on": "2025-12-01", "finished_on": "2026-01-15"},
                    {"started_on": None, "finished_on": "2021-05-01"},
                ],
            },
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["date_read"], "2026-01-15")
        self.assertEqual([event["finished_on"] for event in data["read_events"]], [
            "2026-01-15",
            "2021-05-01",
        ])

    def test_update_book_accepts_start_only_read_event_without_changing_date_read(self):
        resp = self.client.post(
            "/api/books",
            json={
                "title": "Started Again",
                "author": "Author",
                "exclusive_shelf": "currently_reading",
            },
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/books/{book_id}",
            json={"read_events": [{"started_on": "2026-04-16"}]},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["date_read"], "")
        self.assertEqual(data["read_events"][0]["started_on"], "2026-04-16")
        self.assertEqual(data["read_events"][0]["finished_on"], "")

    def test_update_book_rejects_invalid_read_events(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Bad Events", "author": "Author", "exclusive_shelf": "read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        inverted = self.client.put(
            f"/api/books/{book_id}",
            json={"read_events": [{"started_on": "2026-02-01", "finished_on": "2026-01-01"}]},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(inverted.status_code, 422)

    def test_update_book_logs_when_entering_currently_reading_or_read(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Transition Book", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        self.client.put(
            f"/api/books/{book_id}",
            json={"exclusive_shelf": "currently_reading"},
            headers=TEST_AUTH_HEADER,
        )
        self.client.put(
            f"/api/books/{book_id}",
            json={"exclusive_shelf": "read"},
            headers=TEST_AUTH_HEADER,
        )

        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual([item["event_type"] for item in activity], [
            "finished_reading",
            "started_reading",
            "book_added_to_to_read",
        ])

    def test_metadata_only_book_edit_creates_no_activity(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Quiet Book", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/books/{book_id}",
            json={"title": "Quiet Book Revised"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)

        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["event_type"], "book_added_to_to_read")

    def test_update_book_normalizes_shelves(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Tagged Book", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/books/{book_id}",
            json={"shelves": ["to_read", "favorite", "favorite"]},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["shelves"], ["to-read", "favorite"])

    def test_update_book_not_found(self):
        resp = self.client.put(
            "/api/books/99999",
            json={"title": "X"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_book(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "To Delete", "author": "Author"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = resp.json()["id"]

        resp = self.client.delete(
            f"/api/books/{book_id}",
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

    def test_delete_book_not_found(self):
        resp = self.client.delete(
            "/api/books/99999",
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 404)

    def test_activity_pagination(self):
        titles = ["First", "Second", "Third", "Fourth"]
        for title in titles:
            resp = self.client.post(
                "/api/books",
                json={"title": title, "author": "Author", "exclusive_shelf": "to_read"},
                headers=TEST_AUTH_HEADER,
            )
            self.assertEqual(resp.status_code, 201)

        first_page = self.client.get("/api/activity?limit=3&offset=0").json()
        second_page = self.client.get("/api/activity?limit=3&offset=3").json()

        self.assertEqual(len(first_page["items"]), 3)
        self.assertTrue(first_page["pagination"]["has_more"])
        self.assertEqual(first_page["pagination"]["next_offset"], 3)
        self.assertEqual(first_page["items"][0]["summary"], "Added Fourth to to-read")
        self.assertEqual(len(second_page["items"]), 1)
        self.assertFalse(second_page["pagination"]["has_more"])
        self.assertEqual(second_page["items"][0]["summary"], "Added First to to-read")

    def test_deleted_book_activity_has_null_href(self):
        create = self.client.post(
            "/api/books",
            json={"title": "Transient Book", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )
        book_id = create.json()["id"]
        self.client.delete(f"/api/books/{book_id}", headers=TEST_AUTH_HEADER)

        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual(activity[0]["summary"], "Added Transient Book to to-read")
        self.assertIsNone(activity[0]["book"]["href"])

    def test_lookup_endpoint(self):
        mock_results = [{"title": "Dune", "author": "Frank Herbert", "google_books_id": "abc"}]
        with patch("api.google_books.search_books", new_callable=AsyncMock, return_value=mock_results):
            resp = self.client.get("/api/lookup?q=dune")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"][0]["title"], "Dune")

    def test_lookup_requires_query(self):
        resp = self.client.get("/api/lookup?q=")
        self.assertEqual(resp.status_code, 422)

    def test_auth_with_db_token(self):
        """Auth works via auth_tokens table without BOOKSHELF_AUTH_TOKEN env var."""
        resp = self.client.post(
            "/api/books",
            json={"title": "Auth Test", "author": "Author"},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 201)

    def test_auth_rejects_invalid_db_token(self):
        resp = self.client.post(
            "/api/books",
            json={"title": "Auth Test", "author": "Author"},
            headers={"Authorization": "Bearer bad-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_note_logs_note_added(self):
        resp = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "quote", "content": "The sleeper must awaken."},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 201)

        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["event_type"], "note_added")
        self.assertEqual(activity[0]["note_type"], "quote")
        self.assertEqual(activity[0]["summary"], "Added a quote from Dune")

    def test_delete_note_hides_note_added_from_activity(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "First note."},
            headers=TEST_AUTH_HEADER,
        )
        note_id = create.json()["id"]

        delete = self.client.delete(
            f"/api/books/1/notes/{note_id}",
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(self.client.get("/api/activity?limit=10").json()["items"], [])

        conn = get_connection(self.db_path)
        rows = conn.execute(
            "SELECT event_type, note_id FROM activity_log WHERE note_id = ?",
            (note_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "note_added")
        self.assertEqual(rows[0]["note_id"], note_id)
        conn.close()

    def test_delete_note_hides_note_added_from_preview_activity(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "First note."},
            headers=TEST_AUTH_HEADER,
        )
        note_id = create.json()["id"]

        delete = self.client.delete(
            f"/api/books/1/notes/{note_id}",
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(
            self.client.get("/api/activity?view=preview&limit=10").json()["items"],
            [],
        )

    def test_activity_preview_collapses_same_day_notes_per_book(self):
        for content in ("First note.", "Second note.", "Third note."):
            resp = self.client.post(
                "/api/books/1/notes",
                json={"note_type": "thought", "content": content},
                headers=TEST_AUTH_HEADER,
            )
            self.assertEqual(resp.status_code, 201)

        raw = self.client.get("/api/activity?limit=10").json()["items"]
        preview = self.client.get("/api/activity?view=preview&limit=10").json()["items"]

        self.assertEqual(len(raw), 3)
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["event_type"], "note_added")
        self.assertEqual(preview[0]["summary"], "Added 3 notes on Dune")
        self.assertEqual(preview[0]["created_at"], raw[0]["created_at"])
        self.assertNotIn("note_type", preview[0])

    def test_activity_preview_burst_count_shrinks_when_note_is_deleted(self):
        note_ids = []
        for content in ("First note.", "Second note.", "Third note."):
            resp = self.client.post(
                "/api/books/1/notes",
                json={"note_type": "thought", "content": content},
                headers=TEST_AUTH_HEADER,
            )
            self.assertEqual(resp.status_code, 201)
            note_ids.append(resp.json()["id"])

        before_delete = self.client.get("/api/activity?view=preview&limit=10").json()["items"]
        delete = self.client.delete(
            f"/api/books/1/notes/{note_ids[0]}",
            headers=TEST_AUTH_HEADER,
        )
        after_delete = self.client.get("/api/activity?view=preview&limit=10").json()["items"]

        self.assertEqual(delete.status_code, 200)
        self.assertEqual(before_delete[0]["summary"], "Added 3 notes on Dune")
        self.assertEqual(len(after_delete), 1)
        self.assertEqual(after_delete[0]["summary"], "Added 2 notes on Dune")

    def test_activity_preview_does_not_collapse_notes_across_pacific_days(self):
        first = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Late-night note."},
            headers=TEST_AUTH_HEADER,
        )
        second = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Early-morning note."},
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        self._set_note_activity_created_at(first.json()["id"], "2026-04-05T06:30:00Z")
        self._set_note_activity_created_at(second.json()["id"], "2026-04-05T08:30:00Z")

        preview = self.client.get("/api/activity?view=preview&limit=10").json()["items"]

        self.assertEqual(len(preview), 2)
        self.assertEqual(
            [item["summary"] for item in preview],
            ["Added a note on Dune", "Added a note on Dune"],
        )

    def test_activity_preview_does_not_collapse_notes_for_different_books(self):
        first = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Dune note."},
            headers=TEST_AUTH_HEADER,
        )
        second = self.client.post(
            "/api/books/2/notes",
            json={"note_type": "thought", "content": "1984 note."},
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        preview = self.client.get("/api/activity?view=preview&limit=10").json()["items"]

        self.assertEqual(len(preview), 2)
        self.assertEqual(
            {item["summary"] for item in preview},
            {"Added a note on Dune", "Added a note on 1984"},
        )

    def test_activity_preview_does_not_collapse_notes_with_other_event_types(self):
        first = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "First note."},
            headers=TEST_AUTH_HEADER,
        )
        second = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "quote", "content": "Second note."},
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        conn = get_connection(self.db_path)
        activity_id = db_module.insert_activity(
            conn,
            event_type="started_reading",
            book_id=1,
            book_title="Dune",
            book_author="Frank Herbert",
        )
        conn.commit()
        conn.close()

        self._set_note_activity_created_at(first.json()["id"], "2026-04-05T16:00:00Z")
        self._set_note_activity_created_at(second.json()["id"], "2026-04-05T17:00:00Z")
        self._set_activity_created_at(activity_id, "2026-04-05T16:30:00Z")

        preview = self.client.get("/api/activity?view=preview&limit=10").json()["items"]

        self.assertEqual(
            [item["summary"] for item in preview],
            ["Added 2 notes on Dune", "Started Dune"],
        )

    def test_activity_preview_paginates_after_grouping(self):
        dune_first = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Dune first."},
            headers=TEST_AUTH_HEADER,
        )
        dune_second = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Dune second."},
            headers=TEST_AUTH_HEADER,
        )
        nineteen_eighty_four = self.client.post(
            "/api/books/2/notes",
            json={"note_type": "thought", "content": "1984 note."},
            headers=TEST_AUTH_HEADER,
        )
        no_date_book = self.client.post(
            "/api/books/3/notes",
            json={"note_type": "thought", "content": "No Date Book note."},
            headers=TEST_AUTH_HEADER,
        )
        create = self.client.post(
            "/api/books",
            json={"title": "Preview Fourth", "author": "Author", "exclusive_shelf": "to_read"},
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(dune_first.status_code, 201)
        self.assertEqual(dune_second.status_code, 201)
        self.assertEqual(nineteen_eighty_four.status_code, 201)
        self.assertEqual(no_date_book.status_code, 201)
        self.assertEqual(create.status_code, 201)

        self._set_note_activity_created_at(dune_first.json()["id"], "2026-04-05T17:00:00Z")
        self._set_note_activity_created_at(dune_second.json()["id"], "2026-04-05T16:00:00Z")
        self._set_note_activity_created_at(nineteen_eighty_four.json()["id"], "2026-04-05T15:00:00Z")
        self._set_note_activity_created_at(no_date_book.json()["id"], "2026-04-05T14:00:00Z")

        raw = self.client.get("/api/activity?limit=10").json()["items"]
        preview_fourth_id = next(
            item["id"] for item in raw if item["summary"] == "Added Preview Fourth to to-read"
        )
        self._set_activity_created_at(preview_fourth_id, "2026-04-05T13:00:00Z")

        first_page = self.client.get("/api/activity?view=preview&limit=3&offset=0").json()
        second_page = self.client.get("/api/activity?view=preview&limit=3&offset=3").json()

        self.assertEqual(len(first_page["items"]), 3)
        self.assertTrue(first_page["pagination"]["has_more"])
        self.assertEqual(first_page["pagination"]["next_offset"], 3)
        self.assertEqual(
            [item["summary"] for item in first_page["items"]],
            [
                "Added 2 notes on Dune",
                "Added a note on 1984",
                "Added a note on No Date Book",
            ],
        )
        self.assertEqual(len(second_page["items"]), 1)
        self.assertFalse(second_page["pagination"]["has_more"])
        self.assertEqual(second_page["items"][0]["summary"], "Added Preview Fourth to to-read")

    def test_activity_pagination_backfills_past_deleted_note_entries(self):
        for title in ("First", "Second", "Third"):
            resp = self.client.post(
                "/api/books",
                json={"title": title, "author": "Author", "exclusive_shelf": "to_read"},
                headers=TEST_AUTH_HEADER,
            )
            self.assertEqual(resp.status_code, 201)

        create = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Transient note."},
            headers=TEST_AUTH_HEADER,
        )
        note_id = create.json()["id"]
        delete = self.client.delete(
            f"/api/books/1/notes/{note_id}",
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        self.assertEqual(delete.status_code, 200)

        first_page = self.client.get("/api/activity?limit=3&offset=0").json()

        self.assertEqual(len(first_page["items"]), 3)
        self.assertFalse(first_page["pagination"]["has_more"])
        self.assertEqual(
            [item["summary"] for item in first_page["items"]],
            [
                "Added Third to to-read",
                "Added Second to to-read",
                "Added First to to-read",
            ],
        )

    def test_homepage_activity_preview_requests_preview_view(self):
        homepage = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn("fetchJson('/api/activity?view=preview&limit=3')", homepage)

    def test_create_connection_note_enriches_connected_book(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={
                "note_type": "connection",
                "content": "Echoes Orwell's warning.",
                "connected_source_id": 2,
            },
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        notes = self.client.get("/api/books/1/notes").json()["notes"]
        self.assertEqual(notes[0]["note_type"], "connection")
        self.assertEqual(notes[0]["connected_source_id"], 2)
        self.assertIsNone(notes[0]["connected_label"])
        self.assertIsNone(notes[0]["connected_url"])
        self.assertEqual(notes[0]["connected_book"]["title"], "1984")

    def test_connection_note_accepts_external_label_without_book_id(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={
                "note_type": "connection",
                "content": "The same dread as a Bergman film.",
                "connected_label": "Winter Light",
            },
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        notes = self.client.get("/api/books/1/notes").json()["notes"]
        self.assertEqual(notes[0]["note_type"], "connection")
        self.assertIsNone(notes[0]["connected_source_id"])
        self.assertEqual(notes[0]["connected_label"], "Winter Light")
        self.assertIsNone(notes[0]["connected_url"])
        self.assertIsNone(notes[0]["connected_book"])

    def test_connection_note_accepts_external_url(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={
                "note_type": "connection",
                "content": "This has the same stripped spiritual chill.",
                "connected_label": "Winter Light",
                "connected_url": "https://en.wikipedia.org/wiki/Winter_Light",
            },
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(create.status_code, 201)
        notes = self.client.get("/api/books/1/notes").json()["notes"]
        self.assertEqual(notes[0]["connected_label"], "Winter Light")
        self.assertEqual(
            notes[0]["connected_url"],
            "https://en.wikipedia.org/wiki/Winter_Light",
        )
        self.assertIsNone(notes[0]["connected_book"])

    def test_connection_note_requires_connected_book_or_label(self):
        missing = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "connection", "content": "Missing target."},
            headers=TEST_AUTH_HEADER,
        )
        invalid = self.client.post(
            "/api/books/1/notes",
            json={
                "note_type": "connection",
                "content": "Bad target.",
                "connected_source_id": 99999,
            },
            headers=TEST_AUTH_HEADER,
        )
        invalid_url = self.client.post(
            "/api/books/1/notes",
            json={
                "note_type": "connection",
                "content": "Bad URL.",
                "connected_label": "Winter Light",
                "connected_url": "winter-light",
            },
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid_url.status_code, 422)
        self.assertEqual(self.client.get("/api/books/1/notes").json()["notes"], [])

    def test_notes_endpoint_returns_newest_first(self):
        first = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "First note."},
            headers=TEST_AUTH_HEADER,
        )
        second = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "Second note."},
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        notes = self.client.get("/api/books/1/notes").json()["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0]["content"], "Second note.")
        self.assertEqual(notes[1]["content"], "First note.")

    def test_note_update_and_delete_do_not_log_activity(self):
        create = self.client.post(
            "/api/books/1/notes",
            json={"note_type": "thought", "content": "First note."},
            headers=TEST_AUTH_HEADER,
        )
        note_id = create.json()["id"]

        update = self.client.put(
            f"/api/books/1/notes/{note_id}",
            json={"note_type": "thought", "content": "Updated note."},
            headers=TEST_AUTH_HEADER,
        )
        delete = self.client.delete(
            f"/api/books/1/notes/{note_id}",
            headers=TEST_AUTH_HEADER,
        )

        self.assertEqual(update.status_code, 200)
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(self.client.get("/api/activity?limit=10").json()["items"], [])

        conn = get_connection(self.db_path)
        rows = conn.execute(
            "SELECT event_type FROM activity_log WHERE note_id = ? ORDER BY id",
            (note_id,),
        ).fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["note_added"])
        conn.close()

    def test_failed_note_creation_does_not_log_activity(self):
        resp = self.client.post(
            "/api/books/99999/notes",
            json={"note_type": "thought", "content": "Ghost note."},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/api/activity").json()["items"], [])

    def test_create_book_suggestion_persists_row(self):
        resp = self.client.post(
            "/api/book-suggestions",
            json={
                "book_title": "The Magic Mountain",
                "book_author": "Thomas Mann",
                "why": "It feels like it would extend the site’s appetite for reflective, demanding novels.",
                "visitor_name": "Curious reader",
                "visitor_email": "reader@example.com",
            },
            headers={
                "CF-Connecting-IP": "203.0.113.7",
                "User-Agent": "CodexSuggestionTest/1.0",
            },
        )
        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "saved")
        self.assertEqual(payload["delivery_status"], "pending")
        self.mock_send_book_suggestion_notification.assert_not_called()

        conn = get_connection(self.db_path)
        row = get_book_suggestion_by_id(conn, payload["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["book_title"], "The Magic Mountain")
        self.assertEqual(row["book_author"], "Thomas Mann")
        self.assertEqual(row["visitor_name"], "Curious reader")
        self.assertEqual(row["email_status"], "pending")
        self.assertIsNotNone(row["client_ip_hash"])
        self.assertEqual(row["user_agent"], "CodexSuggestionTest/1.0")
        self.assertIsNotNone(row["content_fingerprint"])
        conn.close()

    def test_book_suggestion_duplicate_is_suppressed(self):
        payload = {
            "book_title": "The Magic Mountain",
            "book_author": "Thomas Mann",
            "why": "It feels like the kind of deliberate, searching novel this shelf would care about.",
        }
        headers = {"CF-Connecting-IP": "198.51.100.12"}

        first = self.client.post("/api/book-suggestions", json=payload, headers=headers)
        second = self.client.post("/api/book-suggestions", json=payload, headers=headers)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "already_saved")

        conn = get_connection(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM book_suggestions").fetchone()[0]
        self.assertEqual(count, 1)
        conn.close()

    def test_book_suggestion_rate_limit_blocks_excess_submissions_from_same_ip(self):
        headers = {"CF-Connecting-IP": "198.51.100.44"}

        for idx in range(3):
            resp = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": f"Book {idx}",
                    "why": f"Reason {idx}",
                },
                headers=headers,
            )
            self.assertEqual(resp.status_code, 201)

        blocked = self.client.post(
            "/api/book-suggestions",
            json={
                "book_title": "Book 4",
                "why": "Reason 4",
            },
            headers=headers,
        )

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("wait a little", blocked.json()["detail"])

        conn = get_connection(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM book_suggestions").fetchone()[0]
        self.assertEqual(count, 3)
        conn.close()

    def test_book_suggestion_global_daily_store_limit_blocks_after_limit(self):
        with patch.dict(os.environ, {"BOOK_SUGGESTION_DAILY_STORE_LIMIT": "2"}):
            for idx in range(2):
                resp = self.client.post(
                    "/api/book-suggestions",
                    json={
                        "book_title": f"Daily Book {idx}",
                        "why": f"Reason {idx}",
                    },
                    headers={"CF-Connecting-IP": f"198.51.100.{80 + idx}"},
                )
                self.assertEqual(resp.status_code, 201)

            blocked = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": "Daily Book 3",
                    "why": "Reason 3",
                },
                headers={"CF-Connecting-IP": "198.51.100.99"},
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("today", blocked.json()["detail"])

        conn = get_connection(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM book_suggestions").fetchone()[0]
        self.assertEqual(count, 2)
        conn.close()

    def test_book_suggestion_marks_email_sent_when_delivery_succeeds(self):
        with patch("api.suggestions.get_suggestion_email_config", return_value=object()), patch(
            "api.suggestions.send_book_suggestion_notification"
        ) as send_mock:
            resp = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": "The Magic Mountain",
                    "why": "Worth the detour.",
                    "visitor_email": "reader@example.com",
                },
            )

        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["delivery_status"], "sent")
        send_mock.assert_called_once()

        conn = get_connection(self.db_path)
        row = get_book_suggestion_by_id(conn, payload["id"])
        self.assertEqual(row["email_status"], "sent")
        self.assertIsNotNone(row["email_sent_at"])
        self.assertIsNone(row["email_error"])
        conn.close()

    def test_book_suggestion_daily_email_limit_skips_send_after_limit(self):
        with patch.dict(os.environ, {"BOOK_SUGGESTION_DAILY_EMAIL_LIMIT": "1"}), patch(
            "api.suggestions.get_suggestion_email_config",
            return_value=object(),
        ), patch("api.suggestions.send_book_suggestion_notification") as send_mock:
            first = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": "First Book",
                    "why": "Worth the detour.",
                },
                headers={"CF-Connecting-IP": "198.51.100.120"},
            )
            second = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": "Second Book",
                    "why": "Also worth the detour.",
                },
                headers={"CF-Connecting-IP": "198.51.100.121"},
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["delivery_status"], "sent")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["delivery_status"], "failed")
        self.assertEqual(send_mock.call_count, 1)

        conn = get_connection(self.db_path)
        first_row = get_book_suggestion_by_id(conn, first.json()["id"])
        second_row = get_book_suggestion_by_id(conn, second.json()["id"])
        self.assertEqual(first_row["email_status"], "sent")
        self.assertEqual(second_row["email_status"], "failed")
        self.assertIn("Daily email quota reached", second_row["email_error"])
        conn.close()

    def test_book_suggestion_marks_email_failed_when_delivery_raises(self):
        with patch("api.suggestions.get_suggestion_email_config", return_value=object()), patch(
            "api.suggestions.send_book_suggestion_notification",
            side_effect=RuntimeError("smtp unavailable"),
        ):
            resp = self.client.post(
                "/api/book-suggestions",
                json={
                    "book_title": "The Magic Mountain",
                    "why": "Worth the detour.",
                    "visitor_email": "reader@example.com",
                },
            )

        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["delivery_status"], "failed")

        conn = get_connection(self.db_path)
        row = get_book_suggestion_by_id(conn, payload["id"])
        self.assertEqual(row["email_status"], "failed")
        self.assertIsNone(row["email_sent_at"])
        self.assertIn("smtp unavailable", row["email_error"])
        conn.close()

    def test_book_suggestion_validates_required_fields_and_email(self):
        missing = self.client.post(
            "/api/book-suggestions",
            json={"book_title": "", "why": ""},
        )
        bad_email = self.client.post(
            "/api/book-suggestions",
            json={
                "book_title": "The Magic Mountain",
                "why": "Worth your time.",
                "visitor_email": "not-an-email",
            },
        )

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(bad_email.status_code, 422)

    def test_book_suggestion_honeypot_does_not_persist(self):
        resp = self.client.post(
            "/api/book-suggestions",
            json={
                "book_title": "Spam Book",
                "why": "Ignore this.",
                "website": "https://spam.example",
            },
        )
        self.assertEqual(resp.status_code, 201)

        conn = get_connection(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM book_suggestions").fetchone()[0]
        self.assertEqual(count, 0)
        conn.close()


# ── Tests: Migration script ───────────────────────────────────────────────────

class MigrationScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.books_path, self.llm_path = _make_test_json(self.tempdir.name)
        self.db_path = Path(self.tempdir.name) / "migrated.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_migration_inserts_all_books(self):
        from scripts.migrate_json_to_sqlite import migrate_books, migrate_llm_cache

        conn = get_connection(self.db_path)
        run_migrations(conn)

        counts = migrate_books(conn, SAMPLE_BOOKS_PAYLOAD)
        self.assertEqual(counts["read"], 3)
        self.assertEqual(counts["currently_reading"], 1)
        self.assertEqual(counts["to_read"], 1)

        row = conn.execute("SELECT COUNT(*) as c FROM books").fetchone()
        self.assertEqual(row["c"], 5)
        conn.close()

    def test_migration_maps_my_review_to_review(self):
        from scripts.migrate_json_to_sqlite import migrate_books

        conn = get_connection(self.db_path)
        run_migrations(conn)
        migrate_books(conn, SAMPLE_BOOKS_PAYLOAD)

        row = conn.execute(
            "SELECT review FROM books WHERE title = 'Dune'"
        ).fetchone()
        self.assertEqual(row["review"], "A masterpiece of world-building.")
        conn.close()

    def test_migration_refuses_if_db_exists(self):
        # Create the DB first
        conn = get_connection(self.db_path)
        conn.close()

        from scripts.migrate_json_to_sqlite import main as migrate_main
        import sys

        old_argv = sys.argv
        sys.argv = ["migrate", "--db", str(self.db_path), "--books", str(self.books_path)]
        try:
            result = migrate_main()
            self.assertEqual(result, 1)
        finally:
            sys.argv = old_argv

    def test_migration_llm_cache(self):
        from scripts.migrate_json_to_sqlite import migrate_llm_cache

        conn = get_connection(self.db_path)
        run_migrations(conn)
        migrate_llm_cache(conn, SAMPLE_LLM_CACHE)

        metadata = get_llm_cache_value(conn, "metadata")
        self.assertEqual(metadata["books_hash"], "abc123")

        tp = get_llm_cache_value(conn, "taste_profile")
        self.assertEqual(tp["summary"], "A reader of depth.")
        conn.close()


# ── Tests: Capture API endpoints ─────────────────────────────────────────────

@unittest.skipIf(TestClient is None, "fastapi is not installed")
class ApiCaptureTests(unittest.TestCase):
    """Exercise /api/capture endpoints against a SQLite-backed API."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = _make_test_db(self.tempdir.name)

        self.original_env = os.environ.copy()
        os.environ["DB_PATH"] = str(self.db_path)
        os.environ.pop("BOOKS_DATA", None)
        os.environ.pop("LLM_CACHE_DATA", None)
        os.environ["BOOKSHELF_CORS_ORIGINS"] = "https://book.tanxy.net"
        for key in (
            "BOOK_SUGGESTIONS_TO_EMAIL",
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_FROM_EMAIL",
            "SMTP_USE_STARTTLS",
            "SMTP_USE_SSL",
            "SMTP_TIMEOUT_SECONDS",
        ):
            os.environ.pop(key, None)

        if "api.main" in sys.modules:
            self.api_main = importlib.reload(sys.modules["api.main"])
        else:
            self.api_main = importlib.import_module("api.main")

        self.suggestion_email_config_patcher = patch(
            "api.suggestions.get_suggestion_email_config",
            return_value=None,
        )
        self.suggestion_email_send_patcher = patch(
            "api.suggestions.send_book_suggestion_notification",
        )
        self.mock_get_suggestion_email_config = self.suggestion_email_config_patcher.start()
        self.mock_send_book_suggestion_notification = self.suggestion_email_send_patcher.start()
        self.client = TestClient(self.api_main.app)

    def tearDown(self):
        self.suggestion_email_send_patcher.stop()
        self.suggestion_email_config_patcher.stop()
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tempdir.cleanup()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        # Use a fresh short-lived connection instead of store.conn() — the
        # store connection is pinned to whichever thread first touched it,
        # and FastAPI's TestClient runs endpoints on a different thread.
        return get_connection(self.db_path)

    def _seed_capture(
        self,
        raw_text: str,
        *,
        created_at: str | None = None,
        status: str = "pending",
        resolved_book_id: int | None = None,
        resolved_note_type: str | None = None,
        resolved_content: str | None = None,
        resolved_page_or_location: str | None = None,
        resolved_tags: str | None = None,
        resolved_at: str | None = None,
    ) -> int:
        conn = self._conn()
        try:
            if created_at is None:
                cursor = conn.execute(
                    "INSERT INTO capture_events "
                    "(raw_text, source_channel, status, resolved_book_id, "
                    " resolved_note_type, resolved_content, "
                    " resolved_page_or_location, resolved_tags, resolved_at) "
                    "VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        raw_text,
                        status,
                        resolved_book_id,
                        resolved_note_type,
                        resolved_content,
                        resolved_page_or_location,
                        resolved_tags,
                        resolved_at,
                    ),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO capture_events "
                    "(raw_text, source_channel, status, resolved_book_id, "
                    " resolved_note_type, resolved_content, "
                    " resolved_page_or_location, resolved_tags, "
                    " created_at, resolved_at) "
                    "VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        raw_text,
                        status,
                        resolved_book_id,
                        resolved_note_type,
                        resolved_content,
                        resolved_page_or_location,
                        resolved_tags,
                        created_at,
                        resolved_at,
                    ),
                )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    # ── GET /api/capture ─────────────────────────────────────────────────────

    def test_get_captures_empty_initially(self):
        resp = self.client.get("/api/capture", headers=TEST_AUTH_HEADER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"captures": [], "count": 0})

    def test_get_captures_requires_auth(self):
        resp = self.client.get("/api/capture")
        self.assertEqual(resp.status_code, 401)

    def test_get_captures_rejects_bad_token(self):
        resp = self.client.get(
            "/api/capture",
            headers={"Authorization": "Bearer nope"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_get_captures_defaults_to_pending_only(self):
        self._seed_capture("pending one", created_at="2026-04-10T09:00:00Z")
        self._seed_capture(
            "applied one",
            created_at="2026-04-10T10:00:00Z",
            status="applied",
        )
        self._seed_capture(
            "discarded one",
            created_at="2026-04-10T11:00:00Z",
            status="discarded",
        )

        resp = self.client.get("/api/capture", headers=TEST_AUTH_HEADER)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["captures"]), 1)
        self.assertEqual(data["captures"][0]["raw_text"], "pending one")
        self.assertEqual(data["captures"][0]["status"], "pending")

    def test_get_captures_filters_by_status(self):
        self._seed_capture("p1", created_at="2026-04-10T09:00:00Z")
        self._seed_capture("p2", created_at="2026-04-10T10:00:00Z")
        self._seed_capture(
            "a1",
            created_at="2026-04-10T08:00:00Z",
            status="applied",
        )
        self._seed_capture(
            "d1",
            created_at="2026-04-10T07:00:00Z",
            status="discarded",
        )

        resp = self.client.get(
            "/api/capture?status=applied", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["captures"][0]["raw_text"], "a1")

        resp = self.client.get(
            "/api/capture?status=discarded", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(data["count"], 1)
        self.assertEqual(resp.json()["captures"][0]["raw_text"], "d1")

        resp = self.client.get(
            "/api/capture?status=all", headers=TEST_AUTH_HEADER
        )
        data = resp.json()
        self.assertEqual(data["count"], 4)

    def test_get_captures_orders_oldest_first(self):
        self._seed_capture("third", created_at="2026-04-10T12:00:00Z")
        self._seed_capture("first", created_at="2026-04-10T09:00:00Z")
        self._seed_capture("second", created_at="2026-04-10T10:00:00Z")

        resp = self.client.get("/api/capture", headers=TEST_AUTH_HEADER)
        data = resp.json()
        self.assertEqual(
            [c["raw_text"] for c in data["captures"]],
            ["first", "second", "third"],
        )

    def test_get_captures_rejects_bad_status(self):
        resp = self.client.get(
            "/api/capture?status=bogus", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 422)

    def test_capture_response_shape(self):
        self._seed_capture(
            "shape test",
            created_at="2026-04-10T09:00:00Z",
            resolved_book_id=1,
            resolved_note_type="thought",
            resolved_content="cleaned",
            resolved_page_or_location="p.10",
            resolved_tags='["tagA"]',
        )
        resp = self.client.get("/api/capture", headers=TEST_AUTH_HEADER)
        cap = resp.json()["captures"][0]
        self.assertEqual(
            set(cap.keys()),
            {
                "id",
                "raw_text",
                "source_channel",
                "status",
                "resolved_book_id",
                "resolved_note_type",
                "resolved_content",
                "resolved_page_or_location",
                "resolved_tags",
                "created_at",
                "resolved_at",
            },
        )
        self.assertEqual(cap["source_channel"], "telegram")
        self.assertEqual(cap["resolved_book_id"], 1)
        self.assertEqual(cap["resolved_tags"], '["tagA"]')

    # ── PUT /api/capture/{id} ────────────────────────────────────────────────

    def test_put_updates_resolved_fields(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={
                "resolved_book_id": 1,
                "resolved_note_type": "thought",
                "resolved_content": "cleaned",
                "resolved_page_or_location": "p.171",
                "resolved_tags": ["limit-of-language", "truth"],
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": cid, "status": "updated"})

        row = self._conn().execute(
            "SELECT * FROM capture_events WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(row["resolved_book_id"], 1)
        self.assertEqual(row["resolved_note_type"], "thought")
        self.assertEqual(row["resolved_content"], "cleaned")
        self.assertEqual(row["resolved_page_or_location"], "p.171")
        self.assertEqual(
            json.loads(row["resolved_tags"]),
            ["limit-of-language", "truth"],
        )
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["resolved_at"])

    def test_put_accepts_partial_update_and_preserves_other_fields(self):
        cid = self._seed_capture(
            "raw",
            resolved_book_id=1,
            resolved_note_type="thought",
            resolved_content="original",
        )
        # Send only resolved_content — other fields should be untouched.
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_content": "edited"},
        )
        self.assertEqual(resp.status_code, 200)
        row = self._conn().execute(
            "SELECT * FROM capture_events WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(row["resolved_book_id"], 1)
        self.assertEqual(row["resolved_note_type"], "thought")
        self.assertEqual(row["resolved_content"], "edited")

    def test_put_null_clears_field(self):
        cid = self._seed_capture(
            "raw",
            resolved_book_id=1,
            resolved_note_type="thought",
        )
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_book_id": None},
        )
        self.assertEqual(resp.status_code, 200)
        row = self._conn().execute(
            "SELECT resolved_book_id, resolved_note_type FROM capture_events WHERE id = ?",
            (cid,),
        ).fetchone()
        self.assertIsNone(row["resolved_book_id"])
        self.assertEqual(row["resolved_note_type"], "thought")

    def test_put_404_when_capture_not_found(self):
        resp = self.client.put(
            "/api/capture/9999",
            headers=TEST_AUTH_HEADER,
            json={"resolved_content": "hi"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_put_400_when_capture_already_applied(self):
        cid = self._seed_capture("raw", status="applied")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_content": "too late"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already applied", resp.json()["detail"])

    def test_put_400_when_capture_already_discarded(self):
        cid = self._seed_capture("raw", status="discarded")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_content": "nope"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already discarded", resp.json()["detail"])

    def test_put_422_on_bad_note_type(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_note_type": "rambling"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_404_when_resolved_book_does_not_exist(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_book_id": 99999},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "Book not found.")

    def test_put_422_on_bad_tags_type(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_tags": "not-a-list"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_422_on_tags_with_non_strings(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_tags": ["ok", 42]},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_422_on_bad_book_id_type(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}",
            headers=TEST_AUTH_HEADER,
            json={"resolved_book_id": "not-a-number"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_requires_auth(self):
        cid = self._seed_capture("raw")
        resp = self.client.put(
            f"/api/capture/{cid}", json={"resolved_content": "x"}
        )
        self.assertEqual(resp.status_code, 401)

    # ── POST /api/capture/{id}/apply ─────────────────────────────────────────

    def _seed_ready_to_apply(self) -> int:
        return self._seed_capture(
            "BK p171 — Alyosha feels closer to truth",
            created_at="2026-04-09T22:30:00Z",
            resolved_book_id=1,
            resolved_note_type="thought",
            resolved_content="Alyosha feels closer to truth (edited)",
            resolved_page_or_location="p.171",
            resolved_tags='["limit-of-language"]',
        )

    def test_apply_creates_note_and_marks_capture_applied(self):
        cid = self._seed_ready_to_apply()

        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["capture_id"], cid)
        self.assertEqual(body["status"], "applied")
        note_id = body["note_id"]
        self.assertIsInstance(note_id, int)

        # Note row
        note = self._conn().execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        self.assertIsNotNone(note)
        self.assertEqual(note["source_type"], "book")
        self.assertEqual(note["source_id"], 1)
        self.assertEqual(note["note_type"], "thought")
        self.assertEqual(
            note["content"], "Alyosha feels closer to truth (edited)"
        )
        self.assertEqual(note["page_or_location"], "p.171")
        self.assertEqual(note["tags"], '["limit-of-language"]')

        # Note's created_at comes from the capture, not the apply moment
        self.assertEqual(note["created_at"], "2026-04-09T22:30:00Z")

        # Capture row updated
        cap = self._conn().execute(
            "SELECT status, resolved_at FROM capture_events WHERE id = ?",
            (cid,),
        ).fetchone()
        self.assertEqual(cap["status"], "applied")
        self.assertIsNotNone(cap["resolved_at"])

    def test_apply_shows_up_in_books_notes_endpoint(self):
        cid = self._seed_ready_to_apply()
        apply_resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        note_id = apply_resp.json()["note_id"]

        resp = self.client.get("/api/books/1/notes")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        ids = [n["id"] for n in data["notes"]]
        self.assertIn(note_id, ids)

    def test_apply_logs_activity(self):
        cid = self._seed_ready_to_apply()
        self.client.post(f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER)

        rows = self._conn().execute(
            "SELECT event_type, book_id, note_type FROM activity_log "
            "WHERE event_type = 'note_added'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["book_id"], 1)
        self.assertEqual(rows[0]["note_type"], "thought")

    def test_apply_again_returns_400(self):
        cid = self._seed_ready_to_apply()
        self.client.post(f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER)

        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already applied", resp.json()["detail"])

    def test_apply_400_when_missing_book(self):
        cid = self._seed_capture(
            "raw",
            resolved_note_type="thought",
            resolved_content="edited",
        )
        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resolved_book_id", resp.json()["detail"])

    def test_apply_400_when_missing_note_type(self):
        cid = self._seed_capture(
            "raw", resolved_book_id=1, resolved_content="edited"
        )
        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resolved_note_type", resp.json()["detail"])

    def test_apply_400_when_missing_content(self):
        cid = self._seed_capture(
            "raw", resolved_book_id=1, resolved_note_type="thought"
        )
        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resolved_content", resp.json()["detail"])

    def test_apply_400_when_content_is_whitespace_only(self):
        cid = self._seed_capture(
            "raw",
            resolved_book_id=1,
            resolved_note_type="thought",
            resolved_content="   \n\t  ",
        )
        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)

    def test_apply_400_when_capture_not_found(self):
        resp = self.client.post(
            "/api/capture/9999/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)

    def test_apply_400_when_book_was_deleted(self):
        cid = self._seed_capture(
            "raw",
            resolved_book_id=1,
            resolved_note_type="thought",
            resolved_content="edited",
        )
        self.client.delete("/api/books/1", headers=TEST_AUTH_HEADER)

        resp = self.client.post(
            f"/api/capture/{cid}/apply", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no longer exists", resp.json()["detail"])

    def test_apply_requires_auth(self):
        cid = self._seed_ready_to_apply()
        resp = self.client.post(f"/api/capture/{cid}/apply")
        self.assertEqual(resp.status_code, 401)

    # ── POST /api/capture/{id}/discard ───────────────────────────────────────

    def test_discard_marks_capture_discarded(self):
        cid = self._seed_capture("raw")
        resp = self.client.post(
            f"/api/capture/{cid}/discard", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": cid, "status": "discarded"})

        cap = self._conn().execute(
            "SELECT status, resolved_at FROM capture_events WHERE id = ?",
            (cid,),
        ).fetchone()
        self.assertEqual(cap["status"], "discarded")
        self.assertIsNotNone(cap["resolved_at"])

    def test_discard_does_not_create_note(self):
        cid = self._seed_capture("raw")
        self.client.post(
            f"/api/capture/{cid}/discard", headers=TEST_AUTH_HEADER
        )
        row = self._conn().execute(
            "SELECT COUNT(*) as c FROM notes"
        ).fetchone()
        self.assertEqual(row["c"], 0)

    def test_discard_400_when_already_applied(self):
        cid = self._seed_capture("raw", status="applied")
        resp = self.client.post(
            f"/api/capture/{cid}/discard", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)

    def test_discard_400_when_already_discarded(self):
        cid = self._seed_capture("raw", status="discarded")
        resp = self.client.post(
            f"/api/capture/{cid}/discard", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)

    def test_discard_400_when_capture_not_found(self):
        resp = self.client.post(
            "/api/capture/9999/discard", headers=TEST_AUTH_HEADER
        )
        self.assertEqual(resp.status_code, 400)

    def test_discard_requires_auth(self):
        cid = self._seed_capture("raw")
        resp = self.client.post(f"/api/capture/{cid}/discard")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
