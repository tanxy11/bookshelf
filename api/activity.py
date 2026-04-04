"""Public activity-feed endpoints for the bookshelf API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from db import list_activity_rows

router = APIRouter(prefix="/api/activity")


def _get_deps() -> tuple:
    """Return (store, USE_SQLITE) from app state."""
    from api.main import USE_SQLITE, store

    return store, USE_SQLITE


def _book_href(book_id: int, book_exists: bool) -> str | None:
    return f"book.html?id={book_id}" if book_exists else None


def _summary_for_row(row: dict[str, Any]) -> str:
    title = row.get("book_title") or "Untitled"
    event_type = row.get("event_type")

    if event_type == "book_added_to_to_read":
        return f"Added {title} to to-read"
    if event_type == "started_reading":
        return f"Started {title}"
    if event_type == "finished_reading":
        return f"Finished {title}"
    if event_type == "note_added" and row.get("note_type") == "quote":
        return f"Added a quote from {title}"
    return f"Added a note on {title}"


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "event_type": row["event_type"],
        "created_at": row["created_at"],
        "summary": _summary_for_row(row),
        "book": {
            "id": row["book_id"],
            "title": row["book_title"],
            "author": row["book_author"],
            "href": _book_href(row["book_id"], bool(row["book_exists"])),
        },
    }
    if row.get("note_type"):
        payload["note_type"] = row["note_type"]
    return payload


def _empty_response(limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": [],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": False,
            "next_offset": None,
        },
    }


@router.get("")
async def get_activity(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    store, USE_SQLITE = _get_deps()
    if not USE_SQLITE:
        return _empty_response(limit, offset)

    rows = list_activity_rows(store.conn(), limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    visible_rows = rows[:limit]

    return {
        "items": [_serialize_row(row) for row in visible_rows],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
    }
