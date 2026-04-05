"""SMTP helpers for outbound notification emails."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from bookshelf_data import env_truthy


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    from_email: str
    to_email: str
    username: str | None = None
    password: str | None = None
    use_ssl: bool = False
    use_starttls: bool = True
    timeout_seconds: float = 15.0


def get_suggestion_email_config() -> SmtpConfig | None:
    host = (os.getenv("SMTP_HOST", "") or "").strip()
    from_email = (os.getenv("SMTP_FROM_EMAIL", "") or "").strip()
    to_email = (os.getenv("BOOK_SUGGESTIONS_TO_EMAIL", "") or "").strip()
    if not host or not from_email or not to_email:
        return None

    port_raw = (os.getenv("SMTP_PORT", "") or "").strip()
    try:
        port = int(port_raw) if port_raw else 587
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer.") from exc
    use_ssl = env_truthy("SMTP_USE_SSL", default=False)
    use_starttls = env_truthy("SMTP_USE_STARTTLS", default=not use_ssl)
    timeout_raw = (os.getenv("SMTP_TIMEOUT_SECONDS", "") or "").strip()
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw else 15.0
    except ValueError as exc:
        raise ValueError("SMTP_TIMEOUT_SECONDS must be numeric.") from exc
    username = (os.getenv("SMTP_USERNAME", "") or "").strip() or None
    password = (os.getenv("SMTP_PASSWORD", "") or "").strip() or None

    return SmtpConfig(
        host=host,
        port=port,
        from_email=from_email,
        to_email=to_email,
        username=username,
        password=password,
        use_ssl=use_ssl,
        use_starttls=use_starttls,
        timeout_seconds=timeout_seconds,
    )


def _suggestion_subject(row: dict[str, Any]) -> str:
    return f"Bookshelf suggestion: {row.get('book_title') or 'Untitled'}"


def _suggestion_body(row: dict[str, Any]) -> str:
    title = row.get("book_title") or "Untitled"
    author = row.get("book_author") or "Unknown author"
    why = row.get("why") or ""
    submitted_at = row.get("created_at") or ""
    visitor_name = row.get("visitor_name") or "Anonymous reader"
    visitor_email = row.get("visitor_email") or "No email provided"

    return (
        "A new reading suggestion was submitted on Xinyu's Bookshelf.\n\n"
        f"Suggestion ID: {row.get('id')}\n"
        f"Submitted at: {submitted_at}\n\n"
        f"Book: {title}\n"
        f"Author: {author}\n\n"
        "Why this book?\n"
        f"{why}\n\n"
        f"From: {visitor_name}\n"
        f"Reply email: {visitor_email}\n"
    )


def send_book_suggestion_notification(
    config: SmtpConfig,
    *,
    suggestion_row: dict[str, Any],
) -> None:
    message = EmailMessage()
    message["Subject"] = _suggestion_subject(suggestion_row)
    message["From"] = config.from_email
    message["To"] = config.to_email
    message["X-Bookshelf-Suggestion-ID"] = str(suggestion_row.get("id") or "")

    reply_to = suggestion_row.get("visitor_email")
    if reply_to:
        message["Reply-To"] = str(reply_to)

    message.set_content(_suggestion_body(suggestion_row))

    if config.use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=context,
        ) as server:
            if config.username and config.password:
                server.login(config.username, config.password)
            server.send_message(message)
        return

    with smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds) as server:
        if config.use_starttls:
            context = ssl.create_default_context()
            server.starttls(context=context)
        if config.username and config.password:
            server.login(config.username, config.password)
        server.send_message(message)
