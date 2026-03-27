from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from bookshelf_data import (
    BookshelfDB,
    BookshelfStore,
    default_books_payload,
    default_llm_cache,
    load_env_file,
    load_json,
    save_json,
    successful_recommendations,
    successful_taste_profile,
)
from api.sync import sync_from_rss
from scripts.generate_llm import generate_cache_payload

load_env_file(ROOT_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", "").strip()
BOOKS_DATA_FILE = Path(os.getenv("BOOKS_DATA", "data/books.json"))
LLM_CACHE_FILE = Path(os.getenv("LLM_CACHE_DATA", "data/llm_cache.json"))
GOODREADS_USER_ID = os.getenv("GOODREADS_USER_ID", "")
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


async def refresh_llm_cache(books_path: Path, llm_cache_path: Path) -> dict:
    books_payload = load_json(books_path, default_books_payload)
    cache_payload = load_json(llm_cache_path, default_llm_cache)

    generated_payload, skipped = await generate_cache_payload(books_payload, cache_payload)
    if not skipped:
        save_json(llm_cache_path, generated_payload)

    return {
        "status": "skipped" if skipped else "ok",
        "generated_at": generated_payload.get("generated_at"),
        "books_hash": generated_payload.get("books_hash"),
        "has_taste_profile": successful_taste_profile(generated_payload) is not None,
        "has_recommendations": successful_recommendations(generated_payload) is not None,
    }


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
    if not GOODREADS_USER_ID:
        raise HTTPException(status_code=500, detail="GOODREADS_USER_ID not set.")
    result = await sync_from_rss(GOODREADS_USER_ID, BOOKS_DATA_FILE)
    try:
        result["llm"] = await refresh_llm_cache(BOOKS_DATA_FILE, LLM_CACHE_FILE)
    except Exception as exc:  # noqa: BLE001 - keep RSS sync successful even if LLM refresh fails
        result["llm"] = {"status": "error", "error": str(exc)}
    return result


@app.get("/api/health")
async def health() -> dict:
    payload = store.health()
    payload["environment"] = ENVIRONMENT
    payload["data_backend"] = "sqlite" if USE_SQLITE else "json"
    return payload
