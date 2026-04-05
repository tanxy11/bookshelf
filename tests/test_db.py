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
        self.assertIn("schema_version", tables)
        conn.close()

    def test_schema_version_is_6(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 6)
        conn.close()

    def test_migrations_are_idempotent(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 6)
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

        self.assertEqual(applied, 5)
        self.assertEqual(get_schema_version(conn), 6)
        self.assertIn("notes", tables)
        self.assertIn("activity_log", tables)
        self.assertIn("book_suggestions", tables)
        self.assertIn("connected_label", note_columns)
        self.assertIn("connected_url", note_columns)
        self.assertIn("client_ip_hash", suggestion_columns)
        self.assertIn("user_agent", suggestion_columns)
        self.assertIn("content_fingerprint", suggestion_columns)
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
        # id, notes, cover_url, google_books_id are kept for CRUD/frontend
        self.assertIn("id", book)
        self.assertIn("notes", book)
        # DB-only timestamps are removed
        self.assertNotIn("created_at", book)
        self.assertNotIn("updated_at", book)
        # Empty strings not None
        self.assertIsInstance(book.get("isbn13"), str)
        self.assertIsInstance(book.get("date_read"), str)
        # Shelves should be a list
        self.assertIsInstance(book["shelves"], list)

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

        self.client = TestClient(self.api_main.app)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tempdir.cleanup()

    def test_books_endpoint(self):
        resp = self.client.get("/api/books")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["books"]["read"]), 3)
        self.assertEqual(data["books"]["read"][0]["title"], "Dune")

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
        activity = self.client.get("/api/activity?limit=10").json()["items"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["event_type"], "note_added")

    def test_failed_note_creation_does_not_log_activity(self):
        resp = self.client.post(
            "/api/books/99999/notes",
            json={"note_type": "thought", "content": "Ghost note."},
            headers=TEST_AUTH_HEADER,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/api/activity").json()["items"], [])

    def test_create_book_suggestion_persists_row(self):
        with patch("api.suggestions.get_suggestion_email_config", return_value=None):
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


if __name__ == "__main__":
    unittest.main()
