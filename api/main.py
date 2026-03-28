from __future__ import annotations

import asyncio
import hashlib
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
    compute_books_hash,
    load_env_file,
    utc_now_iso,
)

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


AUTH_TOKEN = os.getenv("BOOKSHELF_AUTH_TOKEN", "").strip()

# ── LLM regeneration state ───────────────────────────────────────────────────
_llm_lock = asyncio.Lock()
_llm_status: dict = {"status": "idle"}


def _verify_auth(request: Request) -> None:
    if not AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Auth token not configured on server.")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = auth[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expected_hash = hashlib.sha256(AUTH_TOKEN.encode()).hexdigest()
    if token_hash != expected_hash:
        raise HTTPException(status_code=401, detail="Invalid token.")


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


@app.get("/api/books")
async def get_books() -> dict:
    books_payload = store.books()
    if not books_payload.get("books", {}).get("read"):
        if USE_SQLITE:
            raise HTTPException(status_code=503, detail="No books found in database.")
        if not BOOKS_DATA_FILE.exists():
            raise HTTPException(status_code=503, detail="books.json not found. Run `make parse` first.")
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


@app.post("/api/sync")
async def sync() -> dict:
    raise HTTPException(
        status_code=410,
        detail="Goodreads RSS sync is deprecated. Books are now managed directly via the database.",
    )


@app.get("/api/llm-status")
async def llm_status() -> dict:
    return _llm_status


@app.post("/api/llm/regenerate")
async def llm_regenerate(request: Request) -> dict:
    _verify_auth(request)
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


@app.get("/api/health")
async def health() -> dict:
    payload = store.health()
    payload["environment"] = ENVIRONMENT
    payload["data_backend"] = "sqlite" if USE_SQLITE else "json"
    return payload
