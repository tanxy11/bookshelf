"""Public book-suggestion submission endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from bookshelf_data import utc_now_iso
from db import (
    count_book_suggestions_since,
    count_sent_book_suggestion_emails_since,
    count_recent_book_suggestions,
    find_recent_duplicate_book_suggestion,
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
WHITESPACE_RE = re.compile(r"\s+")
SHORT_WINDOW = timedelta(minutes=15)
SHORT_WINDOW_LIMIT = 3
DAILY_WINDOW = timedelta(hours=24)
DAILY_WINDOW_LIMIT = 10
DUPLICATE_WINDOW = timedelta(days=7)
GLOBAL_DAILY_STORE_LIMIT = 100
GLOBAL_DAILY_EMAIL_LIMIT = 100
FALLBACK_IP_HASH_SALT = "bookshelf-book-suggestions-dev"


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


def _success_payload(
    *,
    suggestion_id: int | None = None,
    status: str = "saved",
    delivery_status: str = "pending",
    message: str | None = None,
) -> dict[str, Any]:
    if message is None:
        message = "Thanks — I saved that suggestion."
        if delivery_status == "sent":
            message = "Thanks — I saved that suggestion and sent it along."
        elif status == "already_saved":
            message = "Thanks — I already have that suggestion."

    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "delivery_status": delivery_status,
        "message": message,
    }
    if suggestion_id is not None:
        payload["id"] = suggestion_id
    return payload


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _since_iso(window: timedelta) -> str:
    return (_now_utc() - window).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_for_fingerprint(value: str | None) -> str:
    return WHITESPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _content_fingerprint(fields: dict[str, str | None]) -> str:
    normalized = "::".join([
        _normalize_for_fingerprint(fields.get("book_title")),
        _normalize_for_fingerprint(fields.get("why")),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _request_ip_salt() -> str:
    return (
        (os.getenv("BOOK_SUGGESTION_IP_SALT", "") or "").strip()
        or (os.getenv("BOOKSHELF_AUTH_TOKEN", "") or "").strip()
        or FALLBACK_IP_HASH_SALT
    )


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    return max(0, value)


def _extract_client_ip(request: Request) -> str:
    cf_ip = (request.headers.get("cf-connecting-ip", "") or "").strip()
    if cf_ip:
        return cf_ip

    forwarded = (request.headers.get("x-forwarded-for", "") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _client_ip_hash(request: Request) -> str:
    raw_ip = _extract_client_ip(request)
    secret = _request_ip_salt()
    return hashlib.sha256(f"{secret}:{raw_ip}".encode("utf-8")).hexdigest()


def _request_user_agent(request: Request) -> str | None:
    user_agent = (request.headers.get("user-agent", "") or "").strip()
    if not user_agent:
        return None
    return user_agent[:512]


def _enforce_rate_limit(conn, *, client_ip_hash: str) -> None:
    short_count = count_recent_book_suggestions(
        conn,
        client_ip_hash=client_ip_hash,
        since_iso=_since_iso(SHORT_WINDOW),
    )
    if short_count >= SHORT_WINDOW_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="That’s a lot at once — please wait a little before sending another suggestion.",
        )

    daily_count = count_recent_book_suggestions(
        conn,
        client_ip_hash=client_ip_hash,
        since_iso=_since_iso(DAILY_WINDOW),
    )
    if daily_count >= DAILY_WINDOW_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="That’s enough for now — please come back later with another recommendation.",
        )


def _enforce_global_daily_store_limit(conn) -> None:
    daily_store_limit = _int_env(
        "BOOK_SUGGESTION_DAILY_STORE_LIMIT",
        GLOBAL_DAILY_STORE_LIMIT,
    )
    if daily_store_limit <= 0:
        raise HTTPException(
            status_code=429,
            detail="Reading suggestions are closed for now. Please try again later.",
        )

    total_count = count_book_suggestions_since(
        conn,
        since_iso=_since_iso(DAILY_WINDOW),
    )
    if total_count >= daily_store_limit:
        raise HTTPException(
            status_code=429,
            detail="I’ve already received enough suggestions for today. Please try again tomorrow.",
        )


def _existing_duplicate(conn, *, client_ip_hash: str, content_fingerprint: str) -> dict[str, Any] | None:
    return find_recent_duplicate_book_suggestion(
        conn,
        client_ip_hash=client_ip_hash,
        content_fingerprint=content_fingerprint,
        since_iso=_since_iso(DUPLICATE_WINDOW),
    )


def _duplicate_response(existing_id: int | None) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=_success_payload(
            suggestion_id=existing_id,
            status="already_saved",
            delivery_status="already_saved",
        ),
    )


def _persisted_delivery_payload(*, suggestion_id: int, delivery_status: str) -> dict[str, Any]:
    message = "Thanks — I saved that suggestion."
    if delivery_status == "sent":
        message = "Thanks — I saved that suggestion and sent it along."
    return _success_payload(
        suggestion_id=suggestion_id,
        status="saved",
        delivery_status=delivery_status,
        message=message,
    )


def _daily_email_limit_reached(conn) -> bool:
    daily_email_limit = _int_env(
        "BOOK_SUGGESTION_DAILY_EMAIL_LIMIT",
        GLOBAL_DAILY_EMAIL_LIMIT,
    )
    if daily_email_limit <= 0:
        return True
    sent_count = count_sent_book_suggestion_emails_since(
        conn,
        since_iso=_since_iso(DAILY_WINDOW),
    )
    return sent_count >= daily_email_limit


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
    client_ip_hash = _client_ip_hash(request)
    user_agent = _request_user_agent(request)
    content_fingerprint = _content_fingerprint(fields)

    _enforce_rate_limit(conn, client_ip_hash=client_ip_hash)
    duplicate = _existing_duplicate(
        conn,
        client_ip_hash=client_ip_hash,
        content_fingerprint=content_fingerprint,
    )
    if duplicate is not None:
        return _duplicate_response(duplicate.get("id"))

    _enforce_global_daily_store_limit(conn)
    try:
        suggestion_id = insert_book_suggestion(
            conn,
            book_title=fields["book_title"] or "",
            book_author=fields["book_author"],
            why=fields["why"] or "",
            visitor_name=fields["visitor_name"],
            visitor_email=fields["visitor_email"],
            client_ip_hash=client_ip_hash,
            user_agent=user_agent,
            content_fingerprint=content_fingerprint,
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
            if _daily_email_limit_reached(conn):
                update_book_suggestion_email_state(
                    conn,
                    suggestion_id,
                    email_status="failed",
                    email_sent_at=None,
                    email_error="Daily email quota reached before send.",
                )
                conn.commit()
                delivery_status = "failed"
                return _persisted_delivery_payload(
                    suggestion_id=suggestion_id,
                    delivery_status=delivery_status,
                )
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

    return _persisted_delivery_payload(
        suggestion_id=suggestion_id,
        delivery_status=delivery_status,
    )
