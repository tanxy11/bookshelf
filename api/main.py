from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from bookshelf_data import BookshelfStore, load_env_file

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR / ".env")

BOOKS_DATA_FILE = Path(os.getenv("BOOKS_DATA", "data/books.json"))
LLM_CACHE_FILE = Path(os.getenv("LLM_CACHE_DATA", "data/llm_cache.json"))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "BOOKSHELF_CORS_ORIGINS",
        "https://book.tanxy.net,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

store = BookshelfStore(BOOKS_DATA_FILE, LLM_CACHE_FILE)

app = FastAPI(title="Bookshelf API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/books")
async def get_books() -> dict:
    books_payload = store.books()
    if not books_payload.get("books", {}).get("read") and not BOOKS_DATA_FILE.exists():
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


@app.get("/api/health")
async def health() -> dict:
    return store.health()
