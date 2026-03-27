"""Tests for db.py and the JSON-to-SQLite migration."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bookshelf_data import (
    BookshelfDB,
    BookshelfStore,
    compute_books_hash,
    default_books_payload,
    default_llm_cache,
)
from db import (
    get_connection,
    get_llm_cache_value,
    get_schema_version,
    insert_book,
    run_migrations,
    set_llm_cache_value,
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
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_test_db(tmp_dir: str) -> Path:
    """Create a SQLite DB with sample data and return its path."""
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
        self.assertIn("schema_version", tables)
        conn.close()

    def test_schema_version_is_1(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 1)
        conn.close()

    def test_migrations_are_idempotent(self):
        conn = get_connection(self.db_path)
        run_migrations(conn)
        run_migrations(conn)
        self.assertEqual(get_schema_version(conn), 1)
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
        # Should not have DB-only fields
        self.assertNotIn("id", book)
        self.assertNotIn("notes", book)
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

    def test_health_endpoint(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["has_books"])
        self.assertTrue(data["has_taste_profile"])


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
