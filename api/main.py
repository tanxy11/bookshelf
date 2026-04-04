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
    load_env_file,
    utc_now_iso,
)
from api.activity import router as activity_router
from api.auth import verify_auth
from api.notes import router as notes_router

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
app.include_router(notes_router)
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


# ── LLM regeneration state ──────────────────────────────────────────────────
_llm_lock = asyncio.Lock()
_llm_status: dict = {"status": "idle"}


async def _run_llm_regeneration(force: bool = False) -> None:
    global _llm_status
    if not USE_SQLITE:
        _llm_status = {"status": "error", "error": "LLM regeneration requires SQLite backend."}
        return

    _llm_status = {"status": "running", "started_at": utc_now_iso()}
    try:
        books_payload = store.books()
        cache_payload = store.llm_cache()

        from scripts.generate_llm import generate_cache_payload, _save_llm_cache_to_db

        generated, skipped = await generate_cache_payload(books_payload, cache_payload, force=force)
        if skipped:
            _llm_status = {"status": "idle", "skipped": True, "books_hash": generated.get("books_hash")}
            return

        _save_llm_cache_to_db(store.conn(), generated)
        _llm_status = {
            "status": "idle",
            "completed_at": utc_now_iso(),
            "books_hash": generated.get("books_hash"),
        }
    except Exception as exc:
        _llm_status = {"status": "error", "error": str(exc), "failed_at": utc_now_iso()}


def _maybe_trigger_llm_regen(shelf: str) -> None:
    """Fire-and-forget LLM regeneration if a write touched the read shelf."""
    if shelf != "read" or not USE_SQLITE or _llm_lock.locked():
        return

    async def _run() -> None:
        async with _llm_lock:
            await _run_llm_regeneration()

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        pass


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
        rows = store.conn().execute(
            "SELECT source_id, COUNT(*) as cnt FROM notes "
            "WHERE source_type = 'book' GROUP BY source_id"
        ).fetchall()
        counts = {row["source_id"]: row["cnt"] for row in rows}
        for shelf in books_payload.get("books", {}).values():
            if isinstance(shelf, list):
                for book in shelf:
                    book["note_count"] = counts.get(book.get("id", 0), 0)

    return books_payload


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
        "notes": body.get("notes") or None,
        "cover_url": body.get("cover_url") or None,
        "google_books_id": body.get("google_books_id") or None,
        "goodreads_id": body.get("goodreads_id") or None,
    }

    conn = store.conn()
    try:
        book_id = insert_book(conn, book_data)
        book = get_book_by_id(conn, book_id)
        if book is None:
            raise HTTPException(status_code=500, detail="Book was created but could not be loaded.")
        _log_book_activity(conn, event_type=_created_book_activity_type(shelf), book=book)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _maybe_trigger_llm_regen(shelf)
    return book


@app.put("/api/books/{book_id}")
async def update_book_endpoint(book_id: int, request: Request) -> dict:
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="CRUD requires SQLite backend.")

    from db import get_book_by_id, update_book

    existing = get_book_by_id(store.conn(), book_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    body = await request.json()
    if not body:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if "shelves" in body:
        body["shelves"] = _normalize_shelves(body.get("shelves"))

    conn = store.conn()
    try:
        if not update_book(conn, book_id, body):
            raise HTTPException(status_code=404, detail="Book not found.")
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
    except Exception:
        conn.rollback()
        raise

    if old_shelf == "read" or new_shelf == "read":
        _maybe_trigger_llm_regen("read")
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

    shelf = existing.get("exclusive_shelf", "")
    conn = store.conn()
    try:
        delete_book(conn, book_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _maybe_trigger_llm_regen(shelf)

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
    try:
        body = await request.json()
        force = bool(body.get("force"))
    except Exception:
        pass

    async def _run() -> None:
        async with _llm_lock:
            await _run_llm_regeneration(force=force)

    asyncio.create_task(_run())
    return {"status": "started"}


# ── Health endpoint ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    payload = store.health()
    payload["environment"] = ENVIRONMENT
    payload["data_backend"] = "sqlite" if USE_SQLITE else "json"
    return payload
