"""Public book-suggestion submission endpoint."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from bookshelf_data import utc_now_iso
from db import (
    get_book_suggestion_by_id,
    insert_book_suggestion,
    update_book_suggestion_email_state,
)
from api.email_delivery import (
    get_suggestion_email_config,
    send_book_suggestion_notification,
)

router = APIRouter(prefix="/api/book-suggestions")
logger = logging.getLogger(__name__)

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


def _success_payload(*, suggestion_id: int | None = None, delivery_status: str = "pending") -> dict[str, Any]:
    message = "Thanks — I saved that suggestion."
    if delivery_status == "sent":
        message = "Thanks — I saved that suggestion and sent it along."

    payload: dict[str, Any] = {
        "ok": True,
        "status": "saved",
        "delivery_status": delivery_status,
        "message": message,
    }
    if suggestion_id is not None:
        payload["id"] = suggestion_id
    return payload


@router.post("", status_code=201)
async def create_book_suggestion(request: Request) -> dict[str, Any]:
    store, USE_SQLITE = _get_deps()
    if not USE_SQLITE:
        raise HTTPException(status_code=400, detail="Book suggestions require SQLite backend.")

    body = await request.json()
    fields = _validate_body(body)

    # Honeypot for lightweight bot filtering.
    if fields["website"]:
        return _success_payload()

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

    delivery_status = "pending"
    try:
        config = get_suggestion_email_config()
    except Exception as exc:
        logger.warning("Suggestion email delivery is misconfigured: %s", exc)
        config = None
    if config is not None:
        row = get_book_suggestion_by_id(conn, suggestion_id)
        if row is not None:
            try:
                send_book_suggestion_notification(config, suggestion_row=row)
                update_book_suggestion_email_state(
                    conn,
                    suggestion_id,
                    email_status="sent",
                    email_sent_at=utc_now_iso(),
                    email_error=None,
                )
                conn.commit()
                delivery_status = "sent"
            except Exception as exc:
                update_book_suggestion_email_state(
                    conn,
                    suggestion_id,
                    email_status="failed",
                    email_sent_at=None,
                    email_error=str(exc)[:1000],
                )
                conn.commit()
                delivery_status = "failed"
                logger.warning(
                    "Failed to send suggestion email for suggestion_id=%s: %s",
                    suggestion_id,
                    exc,
                )

    return _success_payload(suggestion_id=suggestion_id, delivery_status=delivery_status)
