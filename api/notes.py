"""Notes CRUD endpoints for the bookshelf API."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/books/{book_id}/notes")

VALID_NOTE_TYPES = ("thought", "quote", "connection", "disagreement", "question")


def _get_deps(request: Request) -> tuple:
    """Return (store, USE_SQLITE, _auth) from app state."""
    from api.main import store, USE_SQLITE, _auth
    return store, USE_SQLITE, _auth


def _validate_note_body(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a note request body."""
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content is required and must not be empty.")

    note_type = body.get("note_type", "thought")
    if note_type not in VALID_NOTE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"note_type must be one of: {', '.join(VALID_NOTE_TYPES)}",
        )

    tags = body.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise HTTPException(status_code=422, detail="tags must be a list of strings.")

    connected_source_id = body.get("connected_source_id")
    connected_source_type = "book" if connected_source_id is not None else None

    return {
        "note_type": note_type,
        "content": content,
        "page_or_location": body.get("page_or_location"),
        "connected_source_type": connected_source_type,
        "connected_source_id": connected_source_id,
        "tags": json.dumps(tags, ensure_ascii=False) if tags is not None else None,
    }


def _row_to_note(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row dict to the API response format."""
    tags_raw = row.get("tags")
    tags = json.loads(tags_raw) if tags_raw else []
    return {
        "id": row["id"],
        "note_type": row["note_type"],
        "content": row["content"],
        "page_or_location": row["page_or_location"],
        "connected_source_type": row["connected_source_type"],
        "connected_source_id": row["connected_source_id"],
        "connected_book": None,
        "tags": tags,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _enrich_connected_book(note: dict[str, Any], conn) -> None:
    """If this is a connection note, look up the connected book."""
    if note["note_type"] != "connection" or note["connected_source_id"] is None:
        return
    row = conn.execute(
        "SELECT id, title, author, cover_url FROM books WHERE id = ?",
        (note["connected_source_id"],),
    ).fetchone()
    if row:
        note["connected_book"] = {
            "id": row["id"],
            "title": row["title"],
            "author": row["author"],
            "cover_url": row["cover_url"],
        }


@router.get("")
async def get_notes(book_id: int) -> dict:
    store, USE_SQLITE, _auth = _get_deps(None)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Notes require SQLite backend.")

    conn = store.conn()

    # Verify book exists
    book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    rows = conn.execute(
        "SELECT * FROM notes WHERE source_type = 'book' AND source_id = ? ORDER BY created_at ASC",
        (book_id,),
    ).fetchall()

    notes = [_row_to_note(dict(r)) for r in rows]
    for note in notes:
        _enrich_connected_book(note, conn)

    return {"book_id": book_id, "notes": notes, "count": len(notes)}


@router.post("", status_code=201)
async def create_note(book_id: int, request: Request) -> dict:
    store, USE_SQLITE, _auth = _get_deps(request)
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Notes require SQLite backend.")

    conn = store.conn()

    # Verify book exists
    book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    body = await request.json()
    fields = _validate_note_body(body)

    cursor = conn.execute(
        """INSERT INTO notes (source_type, source_id, note_type, content,
           page_or_location, connected_source_type, connected_source_id, tags)
           VALUES ('book', ?, ?, ?, ?, ?, ?, ?)""",
        (
            book_id,
            fields["note_type"],
            fields["content"],
            fields["page_or_location"],
            fields["connected_source_type"],
            fields["connected_source_id"],
            fields["tags"],
        ),
    )
    conn.commit()
    return {"id": cursor.lastrowid, "status": "created"}


@router.put("/{note_id}")
async def update_note(book_id: int, note_id: int, request: Request) -> dict:
    store, USE_SQLITE, _auth = _get_deps(request)
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Notes require SQLite backend.")

    conn = store.conn()

    # Verify note exists and belongs to this book
    existing = conn.execute(
        "SELECT id FROM notes WHERE id = ? AND source_type = 'book' AND source_id = ?",
        (note_id, book_id),
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Note not found.")

    body = await request.json()
    fields = _validate_note_body(body)

    conn.execute(
        """UPDATE notes SET note_type = ?, content = ?, page_or_location = ?,
           connected_source_type = ?, connected_source_id = ?, tags = ?,
           updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
           WHERE id = ?""",
        (
            fields["note_type"],
            fields["content"],
            fields["page_or_location"],
            fields["connected_source_type"],
            fields["connected_source_id"],
            fields["tags"],
            note_id,
        ),
    )
    conn.commit()
    return {"id": note_id, "status": "updated"}


@router.delete("/{note_id}")
async def delete_note(book_id: int, note_id: int, request: Request) -> dict:
    store, USE_SQLITE, _auth = _get_deps(request)
    _auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Notes require SQLite backend.")

    conn = store.conn()

    # Verify note exists and belongs to this book
    existing = conn.execute(
        "SELECT id FROM notes WHERE id = ? AND source_type = 'book' AND source_id = ?",
        (note_id, book_id),
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Note not found.")

    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return {"id": note_id, "status": "deleted"}
