"""Capture event endpoints for the mobile capture flow.

Captures are raw text messages that arrive from Telegram (or any other
capture channel) and land in the `capture_events` table as `pending`.
The triage UI reads them here, edits the resolved fields, and either
applies them (creates a real note) or discards them.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from db import insert_activity

router = APIRouter(prefix="/api/capture")

VALID_NOTE_TYPES = ("thought", "quote", "connection", "disagreement", "question")
VALID_STATUSES = ("pending", "applied", "discarded", "all")


def _get_deps() -> tuple:
    """Return (store, USE_SQLITE, _auth) from app state."""
    from api.main import USE_SQLITE, _auth, store

    return store, USE_SQLITE, _auth


def _require_sqlite(use_sqlite: bool) -> None:
    if not use_sqlite:
        raise HTTPException(
            status_code=400,
            detail="Capture events require SQLite backend.",
        )


def _row_to_capture(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "raw_text": row["raw_text"],
        "source_channel": row["source_channel"],
        "status": row["status"],
        "resolved_book_id": row["resolved_book_id"],
        "resolved_note_type": row["resolved_note_type"],
        "resolved_content": row["resolved_content"],
        "resolved_page_or_location": row["resolved_page_or_location"],
        "resolved_tags": row["resolved_tags"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def _serialize_resolved_tags(raw: Any) -> str | None:
    """Normalize incoming resolved_tags to a JSON string or None."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not all(isinstance(t, str) for t in raw):
            raise HTTPException(
                status_code=422,
                detail="resolved_tags must be a list of strings.",
            )
        return json.dumps(raw, ensure_ascii=False)
    raise HTTPException(
        status_code=422,
        detail="resolved_tags must be a list of strings or null.",
    )


@router.get("")
async def list_captures(
    request: Request,
    status: str = Query(default="pending"),
) -> dict:
    store, use_sqlite, _auth = _get_deps()
    _auth(request)
    _require_sqlite(use_sqlite)

    if status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {', '.join(VALID_STATUSES)}",
        )

    conn = store.conn()
    if status == "all":
        rows = conn.execute(
            "SELECT * FROM capture_events "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE status = ? "
            "ORDER BY created_at ASC, id ASC",
            (status,),
        ).fetchall()

    captures = [_row_to_capture(dict(r)) for r in rows]
    return {"captures": captures, "count": len(captures)}


@router.put("/{capture_id}")
async def update_capture(capture_id: int, request: Request) -> dict:
    store, use_sqlite, _auth = _get_deps()
    _auth(request)
    _require_sqlite(use_sqlite)

    conn = store.conn()
    row = conn.execute(
        "SELECT id, status FROM capture_events WHERE id = ?",
        (capture_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Capture is already {row['status']}; cannot modify.",
        )

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422, detail="Body must be a JSON object."
        )

    # Partial update: only columns present in the body are touched.
    # Explicit null clears a field.
    updates: list[tuple[str, Any]] = []

    if "resolved_book_id" in body:
        val = body["resolved_book_id"]
        if val is None:
            updates.append(("resolved_book_id", None))
        else:
            try:
                book_id = int(val)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="resolved_book_id must be an integer or null.",
                )
            book = conn.execute(
                "SELECT id FROM books WHERE id = ?",
                (book_id,),
            ).fetchone()
            if book is None:
                raise HTTPException(status_code=404, detail="Book not found.")
            updates.append(("resolved_book_id", book_id))

    if "resolved_note_type" in body:
        val = body["resolved_note_type"]
        if val is None:
            updates.append(("resolved_note_type", None))
        else:
            if val not in VALID_NOTE_TYPES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "resolved_note_type must be one of: "
                        + ", ".join(VALID_NOTE_TYPES)
                    ),
                )
            updates.append(("resolved_note_type", val))

    if "resolved_content" in body:
        val = body["resolved_content"]
        updates.append(("resolved_content", str(val) if val is not None else None))

    if "resolved_page_or_location" in body:
        val = body["resolved_page_or_location"]
        updates.append(
            ("resolved_page_or_location", str(val) if val is not None else None)
        )

    if "resolved_tags" in body:
        updates.append(
            ("resolved_tags", _serialize_resolved_tags(body["resolved_tags"]))
        )

    if not updates:
        return {"id": capture_id, "status": "updated"}

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values: list[Any] = [val for _, val in updates]
    values.append(capture_id)

    try:
        conn.execute(
            f"UPDATE capture_events SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"id": capture_id, "status": "updated"}


@router.post("/{capture_id}/apply", status_code=201)
async def apply_capture(capture_id: int, request: Request) -> dict:
    store, use_sqlite, _auth = _get_deps()
    _auth(request)
    _require_sqlite(use_sqlite)

    conn = store.conn()
    row = conn.execute(
        "SELECT * FROM capture_events WHERE id = ?",
        (capture_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Capture not found.")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Capture is already {row['status']}; cannot apply.",
        )

    resolved_book_id = row["resolved_book_id"]
    resolved_note_type = row["resolved_note_type"]
    resolved_content_raw = row["resolved_content"] or ""
    resolved_content = resolved_content_raw.strip()

    if resolved_book_id is None:
        raise HTTPException(
            status_code=400,
            detail="resolved_book_id must be set before applying.",
        )
    if resolved_note_type is None:
        raise HTTPException(
            status_code=400,
            detail="resolved_note_type must be set before applying.",
        )
    if not resolved_content:
        raise HTTPException(
            status_code=400,
            detail="resolved_content must be set and non-empty before applying.",
        )

    book = conn.execute(
        "SELECT id, title, author FROM books WHERE id = ?",
        (resolved_book_id,),
    ).fetchone()
    if book is None:
        raise HTTPException(
            status_code=400,
            detail="Resolved book no longer exists.",
        )

    try:
        cursor = conn.execute(
            """INSERT INTO notes
               (source_type, source_id, note_type, content,
                page_or_location, tags, created_at, updated_at)
               VALUES ('book', ?, ?, ?, ?, ?, ?, ?)""",
            (
                resolved_book_id,
                resolved_note_type,
                resolved_content,
                row["resolved_page_or_location"],
                row["resolved_tags"],
                row["created_at"],
                row["created_at"],
            ),
        )
        note_id = cursor.lastrowid
        insert_activity(
            conn,
            event_type="note_added",
            book_id=book["id"],
            note_id=note_id,
            book_title=book["title"],
            book_author=book["author"],
            note_type=resolved_note_type,
        )
        conn.execute(
            "UPDATE capture_events SET status = 'applied', "
            "resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ?",
            (capture_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"capture_id": capture_id, "note_id": note_id, "status": "applied"}


@router.post("/{capture_id}/discard")
async def discard_capture(capture_id: int, request: Request) -> dict:
    store, use_sqlite, _auth = _get_deps()
    _auth(request)
    _require_sqlite(use_sqlite)

    conn = store.conn()
    row = conn.execute(
        "SELECT id, status FROM capture_events WHERE id = ?",
        (capture_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Capture not found.")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Capture is already {row['status']}; cannot discard.",
        )

    try:
        conn.execute(
            "UPDATE capture_events SET status = 'discarded', "
            "resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ?",
            (capture_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"id": capture_id, "status": "discarded"}
