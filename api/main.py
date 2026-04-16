from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from bookshelf_data import (
    BookshelfDB,
    BookshelfStore,
    LLM_TARGET_KEYS,
    load_env_file,
    utc_now_iso,
)
from api.activity import router as activity_router
from api.auth import verify_auth
from api.capture import router as capture_router
from api.notes import router as notes_router
from api.suggestions import router as suggestions_router

load_env_file(ROOT_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", "").strip()
BOOKS_DATA_FILE = Path(os.getenv("BOOKS_DATA", "data/books.json"))
LLM_CACHE_FILE = Path(os.getenv("LLM_CACHE_DATA", "data/llm_cache.json"))
ENVIRONMENT = (os.getenv("ENVIRONMENT", "production") or "production").strip()
configured_origins = [
    origin.strip()
    for origin in os.getenv(
        "BOOKSHELF_CORS_ORIGINS",
        "https://book.tanxy.net,https://dev.book.tanxy.net,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]
dev_origins = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8010",
    "http://127.0.0.1:8010",
]
CORS_ORIGINS = list(dict.fromkeys([*configured_origins, *dev_origins]))

USE_SQLITE = bool(DB_PATH and Path(DB_PATH).exists())
if USE_SQLITE:
    store = BookshelfDB(Path(DB_PATH))
else:
    store = BookshelfStore(BOOKS_DATA_FILE, LLM_CACHE_FILE)

app = FastAPI(title="Bookshelf API")
app.include_router(activity_router)
app.include_router(capture_router)
app.include_router(notes_router)
app.include_router(suggestions_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ── Auth helper ──────────────────────────────────────────────────────────────

def _auth(request: Request) -> None:
    conn = store.conn() if USE_SQLITE else None
    verify_auth(request, conn)


def _normalize_shelf_tag(value: str) -> str:
    shelf = (value or "").strip()
    return {
        "to_read": "to-read",
        "currently_reading": "currently-reading",
    }.get(shelf, shelf)


def _normalize_shelves(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = _normalize_shelf_tag(str(raw or "").strip())
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    return normalized


def _created_book_activity_type(shelf: str) -> str | None:
    return {
        "to_read": "book_added_to_to_read",
        "currently_reading": "started_reading",
        "read": "finished_reading",
    }.get(shelf)


def _transition_book_activity_type(old_shelf: str, new_shelf: str) -> str | None:
    if old_shelf == new_shelf:
        return None
    if new_shelf == "currently_reading":
        return "started_reading"
    if new_shelf == "read":
        return "finished_reading"
    return None


def _log_book_activity(conn, *, event_type: str | None, book: dict) -> None:
    if not event_type:
        return

    from db import insert_activity

    insert_activity(
        conn,
        event_type=event_type,
        book_id=book["id"],
        book_title=book.get("title") or "Untitled",
        book_author=book.get("author") or "Unknown author",
    )


def _books_from_payload(books_payload: dict) -> list[dict]:
    books = books_payload.get("books", {}) if isinstance(books_payload, dict) else {}
    return [
        *((books.get("read") or []) if isinstance(books.get("read"), list) else []),
        *((books.get("currently_reading") or []) if isinstance(books.get("currently_reading"), list) else []),
        *((books.get("to_read") or []) if isinstance(books.get("to_read"), list) else []),
    ]


def _sqlite_note_counts(conn) -> dict[int, int]:
    rows = conn.execute(
        "SELECT source_id, COUNT(*) as cnt FROM notes "
        "WHERE source_type = 'book' GROUP BY source_id"
    ).fetchall()
    return {row["source_id"]: row["cnt"] for row in rows}


# ── LLM regeneration state ──────────────────────────────────────────────────
_llm_lock = asyncio.Lock()
_llm_status: dict = {"status": "idle"}


def _normalize_llm_targets(raw_value: object) -> tuple[str, ...] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, list) or not raw_value:
        raise HTTPException(status_code=422, detail="targets must be a non-empty list.")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_target in raw_value:
        target = str(raw_target or "").strip()
        if target not in LLM_TARGET_KEYS:
            valid = ", ".join(LLM_TARGET_KEYS)
            raise HTTPException(status_code=422, detail=f"Unknown LLM target '{target}'. Valid values: {valid}")
        if target in seen:
            continue
        seen.add(target)
        normalized.append(target)

    if not normalized:
        raise HTTPException(status_code=422, detail="targets must be a non-empty list.")
    return tuple(normalized)


def _llm_target_errors(payload: dict, targets: tuple[str, ...]) -> dict[str, str]:
    errors: dict[str, str] = {}
    debug_payload = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}

    if "taste_profile" in targets:
        taste_debug = debug_payload.get("taste_profile") if isinstance(debug_payload, dict) else {}
        if isinstance(taste_debug, dict) and taste_debug.get("error"):
            errors["taste_profile"] = str(taste_debug["error"])

    recommendation_debug = (
        debug_payload.get("recommendations")
        if isinstance(debug_payload, dict) and isinstance(debug_payload.get("recommendations"), dict)
        else {}
    )
    for target in targets:
        if target == "taste_profile":
            continue
        entry = recommendation_debug.get(target) if isinstance(recommendation_debug, dict) else {}
        if isinstance(entry, dict) and entry.get("error"):
            errors[target] = str(entry["error"])

    return errors


async def _run_llm_regeneration(force: bool = False, targets: tuple[str, ...] | None = None) -> None:
    global _llm_status
    selected_targets = targets or tuple(LLM_TARGET_KEYS)
    if not USE_SQLITE:
        _llm_status = {
            "status": "error",
            "error": "LLM regeneration requires SQLite backend.",
            "targets": list(selected_targets),
        }
        return

    _llm_status = {
        "status": "running",
        "started_at": utc_now_iso(),
        "targets": list(selected_targets),
    }
    try:
        books_payload = store.books()
        cache_payload = store.llm_cache()

        from scripts.generate_llm import generate_cache_payload, _save_llm_cache_to_db

        selected_providers = None if targets is None else {target for target in selected_targets if target != "taste_profile"}
        refresh_taste_profile = None if targets is None else "taste_profile" in selected_targets
        generated, skipped = await generate_cache_payload(
            books_payload,
            cache_payload,
            force=force,
            selected_providers=selected_providers,
            refresh_taste_profile=refresh_taste_profile,
        )
        if skipped:
            _llm_status = {
                "status": "idle",
                "skipped": True,
                "books_hash": generated.get("books_hash"),
                "targets": list(selected_targets),
            }
            return

        _save_llm_cache_to_db(store.conn(), generated)
        target_errors = _llm_target_errors(generated, selected_targets)
        if target_errors:
            _llm_status = {
                "status": "error",
                "error": "; ".join(f"{target}: {message}" for target, message in target_errors.items()),
                "errors": target_errors,
                "failed_at": utc_now_iso(),
                "books_hash": generated.get("books_hash"),
                "targets": list(selected_targets),
            }
            return

        _llm_status = {
            "status": "idle",
            "completed_at": utc_now_iso(),
            "books_hash": generated.get("books_hash"),
            "targets": list(selected_targets),
        }
    except Exception as exc:
        _llm_status = {
            "status": "error",
            "error": str(exc),
            "failed_at": utc_now_iso(),
            "targets": list(selected_targets),
        }


# ── Read endpoints ───────────────────────────────────────────────────────────

@app.get("/api/books")
async def get_books() -> dict:
    books_payload = store.books()
    if not books_payload.get("books", {}).get("read"):
        if USE_SQLITE:
            raise HTTPException(status_code=503, detail="No books found in database.")
        if not BOOKS_DATA_FILE.exists():
            raise HTTPException(status_code=503, detail="books.json not found. Run `make parse` first.")

    # Enrich with note_count per book
    if USE_SQLITE:
        counts = _sqlite_note_counts(store.conn())
        for shelf in books_payload.get("books", {}).values():
            if isinstance(shelf, list):
                for book in shelf:
                    book["note_count"] = counts.get(book.get("id", 0), 0)

    return books_payload


@app.get("/api/books/{book_id}")
async def get_book(book_id: int) -> dict:
    if USE_SQLITE:
        from db import get_book_by_id

        conn = store.conn()
        book = get_book_by_id(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Book not found.")

        book["note_count"] = _sqlite_note_counts(conn).get(book_id, 0)
        return book

    books_payload = store.books()
    book = next(
        (entry for entry in _books_from_payload(books_payload) if str(entry.get("id", "")) == str(book_id)),
        None,
    )
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found.")
    return book


@app.get("/api/taste-profile")
async def get_taste_profile() -> dict:
    taste_profile = store.taste_profile()
    if taste_profile is None:
        raise HTTPException(status_code=404, detail="Taste profile is unavailable.")
    return taste_profile


@app.get("/api/recommendations")
async def get_recommendations() -> dict:
    recommendations = store.recommendations()
    if recommendations is None:
        raise HTTPException(status_code=404, detail="Recommendations are unavailable.")
    return recommendations


# ── CRUD endpoints ───────────────────────────────────────────────────────────

@app.post("/api/books", status_code=201)
async def create_book(request: Request) -> dict:
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="CRUD requires SQLite backend.")

    body = await request.json()
    title = (body.get("title") or "").strip()
    author = (body.get("author") or "").strip()
    if not title or not author:
        raise HTTPException(status_code=422, detail="title and author are required.")

    from db import get_book_by_id, insert_book

    shelf = body.get("exclusive_shelf", "to_read")
    shelves = _normalize_shelves(body.get("shelves"))
    book_data = {
        "title": title,
        "author": author,
        "isbn13": body.get("isbn13") or None,
        "my_rating": int(body.get("my_rating", 0)),
        "avg_rating": body.get("avg_rating"),
        "pages": body.get("pages"),
        "date_read": body.get("date_read") or None,
        "date_added": body.get("date_added") or utc_now_iso()[:10],
        "shelves": shelves or [_normalize_shelf_tag(shelf)],
        "exclusive_shelf": shelf,
        "review": body.get("my_review") or body.get("review") or None,
        "cover_url": body.get("cover_url") or None,
        "google_books_id": body.get("google_books_id") or None,
        "goodreads_id": body.get("goodreads_id") or None,
    }
    if "read_events" in body:
        book_data["read_events"] = body.get("read_events")

    conn = store.conn()
    try:
        book_id = insert_book(conn, book_data)
        book = get_book_by_id(conn, book_id)
        if book is None:
            raise HTTPException(status_code=500, detail="Book was created but could not be loaded.")
        _log_book_activity(conn, event_type=_created_book_activity_type(shelf), book=book)
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        conn.rollback()
        raise

    return book


@app.put("/api/books/{book_id}")
async def update_book_endpoint(book_id: int, request: Request) -> dict:
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="CRUD requires SQLite backend.")

    from db import get_book_by_id, replace_read_events, update_book

    existing = get_book_by_id(store.conn(), book_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    body = await request.json()
    if not body:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if "shelves" in body:
        body["shelves"] = _normalize_shelves(body.get("shelves"))
    has_read_events = "read_events" in body
    read_events = body.pop("read_events", None)
    if has_read_events:
        body.pop("date_read", None)
    elif "date_read" in body:
        # Read history is now the source of truth for updates. Keeping this out
        # prevents stale single-date clients from desynchronizing the cache field.
        body.pop("date_read", None)

    conn = store.conn()
    try:
        if not update_book(conn, book_id, body):
            raise HTTPException(status_code=404, detail="Book not found.")
        if has_read_events:
            replace_read_events(conn, book_id, read_events)
        updated = get_book_by_id(conn, book_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Book not found.")
        old_shelf = existing.get("exclusive_shelf", "")
        new_shelf = updated.get("exclusive_shelf", old_shelf)
        _log_book_activity(
            conn,
            event_type=_transition_book_activity_type(old_shelf, new_shelf),
            book=updated,
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        conn.rollback()
        raise

    return updated


@app.delete("/api/books/{book_id}")
async def delete_book_endpoint(book_id: int, request: Request) -> dict:
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="CRUD requires SQLite backend.")

    from db import get_book_by_id, delete_book

    existing = get_book_by_id(store.conn(), book_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    conn = store.conn()
    try:
        delete_book(conn, book_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"deleted": True, "id": book_id}


# ── Lookup endpoint ──────────────────────────────────────────────────────────

@app.get("/api/lookup")
async def lookup_books(q: str = "") -> dict:
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Query parameter 'q' is required.")

    from api.google_books import search_books
    results = await search_books(q)
    return {"results": results}


# ── Deprecated endpoint ──────────────────────────────────────────────────────

@app.post("/api/sync")
async def sync() -> dict:
    raise HTTPException(
        status_code=410,
        detail="Goodreads RSS sync is deprecated. Books are now managed directly via the database.",
    )


# ── LLM endpoints ───────────────────────────────────────────────────────────

@app.get("/api/llm-status")
async def llm_status() -> dict:
    return _llm_status


@app.post("/api/llm/regenerate")
async def llm_regenerate(request: Request) -> dict:
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="LLM regeneration requires SQLite backend.")
    if _llm_lock.locked():
        raise HTTPException(status_code=409, detail="LLM regeneration is already running.")

    force = False
    targets: tuple[str, ...] | None = None
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        force = bool(body.get("force"))
        targets = _normalize_llm_targets(body.get("targets"))

    async def _run() -> None:
        async with _llm_lock:
            await _run_llm_regeneration(force=force, targets=targets)

    asyncio.create_task(_run())
    return {"status": "started", "targets": list(targets or LLM_TARGET_KEYS)}


# ── Health endpoint ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    payload = store.health()
    payload["environment"] = ENVIRONMENT
    payload["data_backend"] = "sqlite" if USE_SQLITE else "json"
    return payload
