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
    load_env_file,
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


# TODO: Add LLM cache regeneration when books are added/edited via CRUD endpoints (Phase 5).
# This should be async (BackgroundTasks) and write results to the llm_cache table in SQLite.


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


@app.get("/api/health")
async def health() -> dict:
    payload = store.health()
    payload["environment"] = ENVIRONMENT
    payload["data_backend"] = "sqlite" if USE_SQLITE else "json"
    return payload
