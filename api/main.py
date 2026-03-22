import json
import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from sync import sync_from_rss

DATA_FILE        = Path(os.getenv("BOOKS_DATA", "/var/www/book.tanxy.net/data/books.json"))
GOODREADS_USER_ID = os.getenv("GOODREADS_USER_ID", "")

app = FastAPI(title="Bookshelf API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/books")
async def get_books():
    if not DATA_FILE.exists():
        return Response(status_code=503, content="books.json not found — run a sync first")
    with DATA_FILE.open(encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="application/json")


@app.post("/api/sync")
async def sync():
    if not GOODREADS_USER_ID:
        return Response(status_code=500, content="GOODREADS_USER_ID not set")
    result = await sync_from_rss(GOODREADS_USER_ID, DATA_FILE)
    return result


@app.get("/api/health")
async def health():
    return {
        "status":    "ok",
        "data_file": str(DATA_FILE),
        "has_data":  DATA_FILE.exists(),
        "user_id_set": bool(GOODREADS_USER_ID),
    }
