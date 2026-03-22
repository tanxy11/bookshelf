from __future__ import annotations

import hashlib
import json
import os
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
        "taste_profile": {},
        "recommendations": {
            "opus": {"model": None},
            "gpt45": {"model": None},
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


def compute_books_hash(read_books: list[dict[str, Any]]) -> str:
    fingerprint = sorted(
        [
            [
                (book.get("title") or "").strip(),
                (book.get("author") or "").strip(),
                int(book.get("my_rating") or 0),
                (book.get("my_review") or "").strip(),
            ]
            for book in read_books
        ]
    )
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
            "has_books": bool(books_payload.get("books", {}).get("read")),
            "has_taste_profile": self.taste_profile() is not None,
            "has_recommendations": self.recommendations() is not None,
        }
