"""Public activity-feed endpoints for the bookshelf API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from db import list_activity_rows

router = APIRouter(prefix="/api/activity")

_PREVIEW_TIMEZONE = ZoneInfo("America/Los_Angeles")
_PREVIEW_FETCH_BATCH = 200


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


def _serialize_row(
    row: dict[str, Any],
    *,
    summary: str | None = None,
    include_note_type: bool = True,
) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "event_type": row["event_type"],
        "created_at": row["created_at"],
        "summary": summary or _summary_for_row(row),
        "book": {
            "id": row["book_id"],
            "title": row["book_title"],
            "author": row["book_author"],
            "href": _book_href(row["book_id"], bool(row["book_exists"])),
        },
    }
    if include_note_type and row.get("note_type"):
        payload["note_type"] = row["note_type"]
    return payload


def _is_public_row(row: dict[str, Any]) -> bool:
    return row.get("event_type") != "note_added" or bool(row.get("note_exists"))


def _list_all_activity_rows(conn) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        batch = list_activity_rows(conn, limit=_PREVIEW_FETCH_BATCH, offset=offset)
        rows.extend(batch)
        if len(batch) < _PREVIEW_FETCH_BATCH:
            break
        offset += _PREVIEW_FETCH_BATCH

    return rows


def _note_preview_group_key(row: dict[str, Any]) -> tuple[int, str] | None:
    if row.get("event_type") != "note_added":
        return None

    created_at = row.get("created_at")
    if not created_at:
        return None

    try:
        timestamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None

    local_day = timestamp.astimezone(_PREVIEW_TIMEZONE).date().isoformat()
    return row["book_id"], local_day


def _serialize_preview_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preview_entries: list[dict[str, Any]] = []
    note_group_indexes: dict[tuple[int, str], int] = {}

    for row in rows:
        group_key = _note_preview_group_key(row)
        if group_key is None:
            preview_entries.append({"row": row, "count": 1})
            continue

        existing_index = note_group_indexes.get(group_key)
        if existing_index is None:
            note_group_indexes[group_key] = len(preview_entries)
            preview_entries.append({"row": row, "count": 1})
            continue

        preview_entries[existing_index]["count"] += 1

    items: list[dict[str, Any]] = []
    for entry in preview_entries:
        row = entry["row"]
        count = entry["count"]
        if count > 1:
            title = row.get("book_title") or "Untitled"
            items.append(
                _serialize_row(
                    row,
                    summary=f"Added {count} notes on {title}",
                    include_note_type=False,
                )
            )
            continue
        items.append(_serialize_row(row))

    return items


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
    view: str | None = Query(default=None),
) -> dict[str, Any]:
    store, USE_SQLITE = _get_deps()
    if not USE_SQLITE:
        return _empty_response(limit, offset)

    conn = store.conn()
    requested_view = (view or "").strip().lower()
    rows = [row for row in _list_all_activity_rows(conn) if _is_public_row(row)]

    if requested_view == "preview":
        serialized_items = _serialize_preview_rows(rows)
    else:
        serialized_items = [_serialize_row(row) for row in rows]

    has_more = len(serialized_items) > offset + limit
    visible_items = serialized_items[offset:offset + limit]

    return {
        "items": visible_items,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
    }
