"""Public book-suggestion submission endpoint."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from db import insert_book_suggestion

router = APIRouter(prefix="/api/book-suggestions")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _get_deps() -> tuple:
    """Return (store, USE_SQLITE) from app state."""
    from api.main import USE_SQLITE, store

    return store, USE_SQLITE


def _clean_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > max_length:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be at most {max_length} characters.",
        )
    return text


def _validate_body(body: Any) -> dict[str, str | None]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")

    title = _clean_optional_text(body.get("book_title"), field_name="book_title", max_length=240)
    if title is None:
        raise HTTPException(status_code=422, detail="book_title is required.")

    why = _clean_optional_text(body.get("why"), field_name="why", max_length=4000)
    if why is None:
        raise HTTPException(status_code=422, detail="why is required.")

    visitor_email = _clean_optional_text(
        body.get("visitor_email"),
        field_name="visitor_email",
        max_length=320,
    )
    if visitor_email and not EMAIL_RE.match(visitor_email):
        raise HTTPException(
            status_code=422,
            detail="visitor_email must be a valid email address.",
        )

    return {
        "book_title": title,
        "book_author": _clean_optional_text(
            body.get("book_author"),
            field_name="book_author",
            max_length=240,
        ),
        "why": why,
        "visitor_name": _clean_optional_text(
            body.get("visitor_name"),
            field_name="visitor_name",
            max_length=160,
        ),
        "visitor_email": visitor_email,
        "website": _clean_optional_text(
            body.get("website"),
            field_name="website",
            max_length=240,
        ),
    }


@router.post("", status_code=201)
async def create_book_suggestion(request: Request) -> dict[str, Any]:
    store, USE_SQLITE = _get_deps()
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Book suggestions require SQLite backend.")

    body = await request.json()
    fields = _validate_body(body)

    # Honeypot for lightweight bot filtering.
    if fields["website"]:
        return {
            "ok": True,
            "status": "saved",
            "message": "Thanks — I saved that suggestion.",
        }

    conn = store.conn()
    try:
        suggestion_id = insert_book_suggestion(
            conn,
            book_title=fields["book_title"] or "",
            book_author=fields["book_author"],
            why=fields["why"] or "",
            visitor_name=fields["visitor_name"],
            visitor_email=fields["visitor_email"],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "ok": True,
        "id": suggestion_id,
        "status": "saved",
        "message": "Thanks — I saved that suggestion.",
    }
