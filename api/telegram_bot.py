"""
Telegram capture bot for book.tanxy.net.

Runs as a separate long-lived process alongside the FastAPI server.
Listens for text messages from an allow-listed chat and writes each
message verbatim into the `capture_events` table for later triage
from the desktop inbox UI.

Environment variables (loaded from `.env` at repo root):
    TELEGRAM_BOT_TOKEN        Bot token from @BotFather (required)
    TELEGRAM_ALLOWED_CHAT_ID  Numeric chat ID of the only sender allowed
                              to store captures (required)
    DB_PATH                   Path to the SQLite database
                              (default: data/bookshelf.db)

Run locally:
    .venv/bin/python -m api.telegram_bot
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from bookshelf_data import load_env_file  # noqa: E402

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:  # pragma: no cover — import guard
    print(
        "Error: python-telegram-bot is not installed. "
        "Install with: pip install 'python-telegram-bot>=21.0'",
        file=sys.stderr,
    )
    sys.exit(1)


load_env_file(ROOT_DIR / ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# Quiet the noisy underlying HTTP client; keep our own logger at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bookshelf.telegram_bot")


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID_RAW = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "").strip() or "data/bookshelf.db"

RECENT_LIMIT = 5
RAW_TEXT_PREVIEW_LIMIT = 50


def _die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


if not TOKEN:
    _die("TELEGRAM_BOT_TOKEN is not set")
if not ALLOWED_CHAT_ID_RAW:
    _die("TELEGRAM_ALLOWED_CHAT_ID is not set")

try:
    ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW)
except ValueError:
    _die(
        "TELEGRAM_ALLOWED_CHAT_ID must be an integer, "
        f"got: {ALLOWED_CHAT_ID_RAW!r}"
    )


# ── Database helpers ─────────────────────────────────────────────────────────


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def insert_capture(raw_text: str) -> int:
    """Store a raw Telegram message as a pending capture event. Returns new id."""
    conn = _db_connect()
    try:
        cursor = conn.execute(
            "INSERT INTO capture_events (raw_text, source_channel) VALUES (?, 'telegram')",
            (raw_text,),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def fetch_recent_captures(limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    conn = _db_connect()
    try:
        return conn.execute(
            "SELECT id, status, raw_text FROM capture_events "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def _truncate(text: str, limit: int = RAW_TEXT_PREVIEW_LIMIT) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


# ── Handlers ─────────────────────────────────────────────────────────────────


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the caller's chat id. Intentionally not auth-gated — this
    is how the user discovers their own chat id during initial setup."""
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(
        chat_id=chat.id, text=f"Your chat ID is: {chat.id}"
    )


async def handle_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.id != ALLOWED_CHAT_ID:
        await context.bot.send_message(chat_id=chat.id, text="Unauthorized")
        logger.warning("rejected /recent from unauthorized chat_id=%s", chat.id)
        return

    try:
        rows = fetch_recent_captures()
    except sqlite3.Error:
        logger.exception("failed to fetch recent captures")
        await context.bot.send_message(
            chat_id=chat.id, text="Failed to fetch recent captures."
        )
        return

    if not rows:
        await context.bot.send_message(chat_id=chat.id, text="No captures yet.")
        return

    lines = ["Recent captures:"]
    for row in rows:
        preview = _truncate(row["raw_text"] or "")
        lines.append(f'#{row["id"]} ({row["status"]}) — "{preview}"')
    await context.bot.send_message(chat_id=chat.id, text="\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return

    if chat.id != ALLOWED_CHAT_ID:
        await context.bot.send_message(chat_id=chat.id, text="Unauthorized")
        logger.warning("rejected message from unauthorized chat_id=%s", chat.id)
        return

    raw_text = (message.text or "").strip()
    if not raw_text:
        return

    try:
        capture_id = insert_capture(raw_text)
    except sqlite3.Error:
        logger.exception("failed to insert capture")
        await context.bot.send_message(
            chat_id=chat.id, text="Failed to store capture."
        )
        return

    logger.info("stored capture #%s (%d chars)", capture_id, len(raw_text))
    await context.bot.send_message(
        chat_id=chat.id, text=f"✓ Captured (#{capture_id})"
    )


# ── Entrypoint ───────────────────────────────────────────────────────────────


def build_application() -> Application:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("recent", handle_recent))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    return application


def main() -> None:
    logger.info(
        "starting telegram bot (allowed_chat_id=%s, db_path=%s)",
        ALLOWED_CHAT_ID,
        DB_PATH,
    )
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
