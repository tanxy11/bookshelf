#!/usr/bin/env python3
"""
One-time migration: books.json + llm_cache.json → SQLite database.

Usage:
    python scripts/migrate_json_to_sqlite.py
    python scripts/migrate_json_to_sqlite.py --db data/bookshelf.db
    python scripts/migrate_json_to_sqlite.py --force   # overwrite existing DB
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from hashlib import sha256
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from bookshelf_data import default_llm_cache, load_json, default_books_payload
from db import get_connection, insert_book, run_migrations, set_llm_cache_value


# ── Shelf name normalization ──────────────────────────────────────────────────

SHELF_NORMALIZE = {
    "read": "read",
    "currently-reading": "currently_reading",
    "currently_reading": "currently_reading",
    "to-read": "to_read",
    "to_read": "to_read",
}


def normalize_shelf(raw: str) -> str:
    return SHELF_NORMALIZE.get(raw, raw)


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate_books(conn, books_payload: dict) -> dict[str, int]:
    """Insert all books from books.json into the DB. Returns counts by shelf."""
    counts: dict[str, int] = {}

    for shelf_key in ("read", "currently_reading", "to_read"):
        books = books_payload.get("books", {}).get(shelf_key, [])
        db_shelf = normalize_shelf(shelf_key)
        inserted = 0

        for book in books:
            book_data = {
                "goodreads_id": book.get("goodreads_id") or None,
                "title": book.get("title", ""),
                "author": book.get("author", ""),
                "isbn13": book.get("isbn13") or None,
                "my_rating": book.get("my_rating", 0),
                "avg_rating": book.get("avg_rating"),
                "pages": book.get("pages"),
                "date_read": book.get("date_read") or None,
                "date_added": book.get("date_added") or "",
                "shelves": book.get("shelves", []),
                "exclusive_shelf": db_shelf,
                "review": book.get("my_review") or None,
                "notes": None,
                "cover_url": None,
                "google_books_id": None,
            }
            if isinstance(book.get("read_events"), list):
                book_data["read_events"] = book["read_events"]

            try:
                insert_book(conn, book_data)
                inserted += 1
            except Exception as exc:
                print(
                    f"  Warning: skipped '{book_data['title']}' by "
                    f"'{book_data['author']}': {exc}",
                    file=sys.stderr,
                )

        counts[db_shelf] = inserted

    conn.commit()
    return counts


def migrate_llm_cache(conn, cache_payload: dict) -> None:
    """Migrate llm_cache.json into the llm_cache table."""
    metadata = {
        "books_hash": cache_payload.get("books_hash", ""),
        "generated_at": cache_payload.get("generated_at"),
        "dry_run": cache_payload.get("dry_run", False),
    }
    set_llm_cache_value(conn, "metadata", metadata)

    taste_profile = cache_payload.get("taste_profile")
    if taste_profile:
        set_llm_cache_value(conn, "taste_profile", taste_profile)

    recommendations = cache_payload.get("recommendations")
    if recommendations:
        set_llm_cache_value(conn, "recommendations", recommendations)


def generate_auth_token(conn) -> str:
    """Generate a random auth token, store its hash, return the plain token."""
    token = secrets.token_urlsafe(48)
    token_hash = sha256(token.encode()).hexdigest()
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, label) VALUES (?, ?)",
        (token_hash, "initial-migration"),
    )
    conn.commit()
    return token


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate books.json + llm_cache.json → SQLite."
    )
    parser.add_argument(
        "--db", default="data/bookshelf.db", help="Path to output SQLite DB"
    )
    parser.add_argument(
        "--books", default="data/books.json", help="Path to books.json"
    )
    parser.add_argument(
        "--llm-cache", default="data/llm_cache.json", help="Path to llm_cache.json"
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing database"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    books_path = Path(args.books)
    llm_cache_path = Path(args.llm_cache)

    # Safety check
    if db_path.exists() and not args.force:
        print(
            f"Error: {db_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    if db_path.exists() and args.force:
        db_path.unlink()
        print(f"Removed existing {db_path}")

    # Load source data
    if not books_path.exists():
        print(f"Error: {books_path} not found.", file=sys.stderr)
        return 1

    books_payload = load_json(books_path, default_books_payload)
    cache_payload = load_json(llm_cache_path, default_llm_cache)

    # Create DB and run schema migrations
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    applied = run_migrations(conn)
    print(f"Applied {applied} migration(s) to {db_path}")

    # Migrate books
    counts = migrate_books(conn, books_payload)
    total = sum(counts.values())
    print(f"Inserted {total} books:")
    for shelf, count in counts.items():
        print(f"  {shelf}: {count}")

    # Migrate LLM cache
    migrate_llm_cache(conn, cache_payload)
    print("Migrated LLM cache")

    # Generate auth token
    token = generate_auth_token(conn)
    print(f"\nAuth token (save this — it won't be shown again):")
    print(f"  {token}")
    print(f"\nAdd to your .env file:")
    print(f"  BOOKSHELF_AUTH_TOKEN={token}")

    # Verify
    row = conn.execute("SELECT COUNT(*) as c FROM books").fetchone()
    print(f"\nVerification: {row['c']} total books in database")

    for shelf in ("read", "currently_reading", "to_read"):
        row = conn.execute(
            "SELECT COUNT(*) as c FROM books WHERE exclusive_shelf = ?",
            (shelf,),
        ).fetchone()
        print(f"  {shelf}: {row['c']}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
