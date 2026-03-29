from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_env_file(path: Path, override: bool = False) -> bool:
    if not path.exists():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not override and key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ[key] = value

    return True


def env_truthy(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def default_books_payload() -> dict[str, Any]:
    return {
        "generated_at": None,
        "books": {
            "read": [],
            "currently_reading": [],
            "to_read": [],
        },
        "stats": {
            "total_read": 0,
            "total_to_read": 0,
            "currently_reading_count": 0,
            "avg_my_rating": 0.0,
            "books_this_year": 0,
            "top_authors": [],
        },
    }


def default_llm_cache() -> dict[str, Any]:
    return {
        "books_hash": "",
        "generated_at": None,
        "dry_run": False,
        "prompt_hash": "",
        "taste_profile": {},
        "recommendations": {
            "opus": {"model": None},
            "gpt45": {"model": None},
            "gemini": {"model": None},
        },
    }


def load_json(path: Path, default_factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    if not path.exists():
        return default_factory()
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return merge_defaults(default_factory(), data)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def merge_defaults(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = {key: deepcopy(value) for key, value in base.items()}
        for key, value in override.items():
            merged[key] = merge_defaults(merged[key], value) if key in merged else deepcopy(value)
        return merged
    return deepcopy(override)


def normalize_book_key(title: str, author: str) -> tuple[str, str]:
    return (title or "").strip().lower(), (author or "").strip().lower()


def _book_hash_entry(book: dict[str, Any], shelf_key: str) -> list[Any]:
    return [
        shelf_key,
        (book.get("title") or "").strip(),
        (book.get("author") or "").strip(),
        int(book.get("my_rating") or 0),
        (book.get("my_review") or "").strip(),
        (book.get("date_read") or "").strip(),
        (book.get("date_added") or "").strip(),
        sorted(str(shelf).strip() for shelf in (book.get("shelves") or []) if str(shelf).strip()),
    ]


def compute_books_hash(books_payload: dict[str, Any] | list[dict[str, Any]]) -> str:
    if isinstance(books_payload, dict):
        books_by_shelf = books_payload.get("books", {})
        fingerprint = sorted(
            _book_hash_entry(book, shelf_key)
            for shelf_key in ("read", "currently_reading", "to_read")
            for book in books_by_shelf.get(shelf_key, [])
        )
    else:
        fingerprint = sorted(_book_hash_entry(book, "read") for book in books_payload)

    payload = json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def successful_taste_profile(cache: dict[str, Any]) -> dict[str, Any] | None:
    profile = cache.get("taste_profile")
    if not isinstance(profile, dict) or profile.get("error"):
        return None
    if not profile.get("summary"):
        return None
    return profile


def successful_recommendations(cache: dict[str, Any]) -> dict[str, Any] | None:
    recommendations = cache.get("recommendations")
    if not isinstance(recommendations, dict):
        return None
    if any(isinstance(entry, dict) and entry.get("books") for entry in recommendations.values()):
        return recommendations
    return None


class JsonFileCache:
    def __init__(self, path: Path, default_factory: Callable[[], dict[str, Any]]):
        self.path = path
        self.default_factory = default_factory
        self._mtime_ns: int | None = None
        self._cached: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            self._mtime_ns = None
            self._cached = self.default_factory()
            return deepcopy(self._cached)

        stat = self.path.stat()
        if self._cached is None or self._mtime_ns != stat.st_mtime_ns:
            self._mtime_ns = stat.st_mtime_ns
            self._cached = load_json(self.path, self.default_factory)
        return deepcopy(self._cached)


class BookshelfStore:
    def __init__(self, books_path: Path, llm_cache_path: Path):
        self.books_file = JsonFileCache(books_path, default_books_payload)
        self.llm_cache_file = JsonFileCache(llm_cache_path, default_llm_cache)

    def books(self) -> dict[str, Any]:
        return self.books_file.read()

    def llm_cache(self) -> dict[str, Any]:
        return self.llm_cache_file.read()

    def taste_profile(self) -> dict[str, Any] | None:
        return successful_taste_profile(self.llm_cache())

    def recommendations(self) -> dict[str, Any] | None:
        return successful_recommendations(self.llm_cache())

    def health(self) -> dict[str, Any]:
        books_payload = self.books()
        llm_cache = self.llm_cache()
        return {
            "status": "ok",
            "generated_at": llm_cache.get("generated_at") or books_payload.get("generated_at"),
            "books_generated_at": books_payload.get("generated_at"),
            "llm_generated_at": llm_cache.get("generated_at"),
            "books_hash": llm_cache.get("books_hash"),
            "dry_run": bool(llm_cache.get("dry_run")),
            "has_books": bool(books_payload.get("books", {}).get("read")),
            "has_taste_profile": self.taste_profile() is not None,
            "has_recommendations": self.recommendations() is not None,
        }


class BookshelfDB:
    """SQLite-backed replacement for BookshelfStore.

    Returns the same dict structures so the API and frontend work unchanged.
    """

    def __init__(self, db_path: Path):
        from db import get_connection, run_migrations

        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._get_connection = get_connection
        self._run_migrations = run_migrations

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._get_connection(self.db_path)
            self._run_migrations(self._conn)
        return self._conn

    def _row_to_book(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a DB row to the dict format matching books.json entries."""
        d = dict(row)
        # Keep id for CRUD links; remove other DB-only fields
        d.pop("created_at", None)
        d.pop("updated_at", None)

        # Parse shelves JSON → list
        shelves_raw = d.pop("shelves", None)
        d["shelves"] = json.loads(shelves_raw) if shelves_raw else []

        # DB column is "review" but API returns "my_review"
        d["my_review"] = d.pop("review", None) or ""

        # Normalize types to match JSON conventions (empty string not None)
        d["isbn13"] = d.get("isbn13") or ""
        d["date_read"] = d.get("date_read") or ""
        d["date_added"] = d.get("date_added") or ""
        d["goodreads_id"] = d.get("goodreads_id") or ""
        d["my_rating"] = d.get("my_rating") or 0

        return d

    def _get_books_by_shelf(self, shelf: str) -> list[dict[str, Any]]:
        if shelf == "read":
            order = """
                CASE WHEN date_read IS NOT NULL AND date_read != '' THEN 0 ELSE 1 END,
                date_read DESC, date_added DESC
            """
        elif shelf == "currently_reading":
            order = "date_added DESC, date_read DESC"
        else:
            order = "date_added DESC"

        rows = self.conn().execute(
            f"SELECT * FROM books WHERE exclusive_shelf = ? ORDER BY {order}",
            (shelf,),
        ).fetchall()
        return [self._row_to_book(row) for row in rows]

    def _compute_stats(self, read_books: list[dict[str, Any]],
                       to_read_count: int,
                       currently_reading_count: int) -> dict[str, Any]:
        current_year = datetime.now().year
        rated = [b["my_rating"] for b in read_books if b.get("my_rating", 0) > 0]
        avg = round(sum(rated) / len(rated), 2) if rated else 0.0
        this_year = sum(
            1 for b in read_books
            if (b.get("date_read") or "").startswith(str(current_year))
        )
        top_authors = [
            {"author": a, "count": c}
            for a, c in Counter(
                b["author"] for b in read_books if b.get("author")
            ).most_common(10)
        ]
        return {
            "total_read": len(read_books),
            "books_this_year": this_year,
            "avg_my_rating": avg,
            "top_authors": top_authors,
            "total_to_read": to_read_count,
            "currently_reading_count": currently_reading_count,
        }

    def books(self) -> dict[str, Any]:
        read = self._get_books_by_shelf("read")
        currently_reading = self._get_books_by_shelf("currently_reading")
        to_read = self._get_books_by_shelf("to_read")
        stats = self._compute_stats(read, len(to_read), len(currently_reading))

        # Use the most recent updated_at as generated_at
        row = self.conn().execute(
            "SELECT MAX(updated_at) as latest FROM books"
        ).fetchone()
        generated_at = row["latest"] if row else None

        return {
            "generated_at": generated_at,
            "books": {
                "read": read,
                "currently_reading": currently_reading,
                "to_read": to_read,
            },
            "stats": stats,
        }

    def _get_llm_cache_value(self, key: str) -> dict[str, Any] | None:
        row = self.conn().execute(
            "SELECT value FROM llm_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def llm_cache(self) -> dict[str, Any]:
        """Reconstruct the old llm_cache.json structure from DB rows."""
        metadata = self._get_llm_cache_value("metadata") or {}
        taste_profile = self._get_llm_cache_value("taste_profile") or {}
        recommendations = self._get_llm_cache_value("recommendations") or {
            "opus": {"model": None},
            "gpt45": {"model": None},
            "gemini": {"model": None},
        }

        return {
            "books_hash": metadata.get("books_hash", ""),
            "generated_at": metadata.get("generated_at"),
            "dry_run": metadata.get("dry_run", False),
            "prompt_hash": metadata.get("prompt_hash", ""),
            "taste_profile": taste_profile,
            "recommendations": recommendations,
        }

    def taste_profile(self) -> dict[str, Any] | None:
        return successful_taste_profile(self.llm_cache())

    def recommendations(self) -> dict[str, Any] | None:
        return successful_recommendations(self.llm_cache())

    def health(self) -> dict[str, Any]:
        books_payload = self.books()
        llm_cache = self.llm_cache()
        return {
            "status": "ok",
            "generated_at": llm_cache.get("generated_at") or books_payload.get("generated_at"),
            "books_generated_at": books_payload.get("generated_at"),
            "llm_generated_at": llm_cache.get("generated_at"),
            "books_hash": llm_cache.get("books_hash"),
            "dry_run": bool(llm_cache.get("dry_run")),
            "has_books": bool(books_payload.get("books", {}).get("read")),
            "has_taste_profile": self.taste_profile() is not None,
            "has_recommendations": self.recommendations() is not None,
        }
