# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install         # Create .venv and install dependencies
make parse           # Goodreads CSV → data/books.json (legacy)
make llm             # data/books.json → data/llm_cache.json (legacy, skips if unchanged)
make llm-force       # Always regenerate LLM outputs (legacy)
make build           # parse + llm (legacy pipeline)
make dev             # Run FastAPI (port 8001) + static site (port 8000) locally
make add-notes-table # Create notes table in SQLite DB (idempotent)
make add-capture-table # Create capture_events table in SQLite DB (idempotent, legacy — migration v8 auto-runs on startup)
.venv/bin/python scripts/generate_llm.py --db data/bookshelf.db --provider gemini
                     # Refresh only Gemini recommendations
.venv/bin/python scripts/generate_llm.py --db data/bookshelf.db --with-taste-profile --taste-profile-provider openai
                     # Refresh taste profile with OpenAI instead of Claude
.venv/bin/python -m api.telegram_bot
                     # Run the Telegram capture bot locally (requires TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_CHAT_ID)
```

**Deploy:**
```bash
make deploy          # backup VPS DB → rsync code → restart API → restart telegram bot (if installed)
make deploy-staging  # rsync code → restart staging API
```

**Telegram capture bot (VPS):**
```bash
make bot-status      # SSH: systemctl status bookshelf-telegram-bot
make bot-logs        # SSH: journalctl -u bookshelf-telegram-bot -f
make bot-restart     # SSH: systemctl restart bookshelf-telegram-bot
```

**Database management:**
```bash
make backup          # SSH: snapshot VPS DB to data/backups/bookshelf-{timestamp}.db
make pull-db         # scp VPS DB → local data/bookshelf.db
make push-db         # scp local DB → VPS (interactive confirmation required)
make seed-staging    # SSH: copy prod DB → staging DB on VPS
```

**Run tests:**
```bash
.venv/bin/python -m pytest tests/
# or single file:
.venv/bin/python -m pytest tests/test_generate_llm.py
```

## Architecture

**Data storage:**
- **SQLite** (`data/bookshelf.db`) is the canonical data store when `DB_PATH` is set. Falls back to JSON files otherwise.
- `db.py` — SQLite schema, connection factory (WAL mode), numbered migration system. Migrations run automatically on API startup.
- `scripts/migrate_json_to_sqlite.py` — one-time migration from `books.json` + `llm_cache.json` → SQLite
- `scripts/add_notes_table.py` — idempotent migration that creates the `notes` table (run via `make add-notes-table`)

**Data pipeline (build-time, legacy):**
1. `scripts/parse_goodreads.py` — parses Goodreads CSV export → `data/books.json`
2. `scripts/generate_llm.py` — calls Anthropic + OpenAI + Gemini APIs → `data/llm_cache.json` (skips if SHA-256 hash of read shelf is unchanged)

**Runtime:**
- `api/main.py` — FastAPI server; reads from SQLite via `BookshelfDB` (or JSON via `BookshelfStore` as fallback)
- `api/auth.py` — Bearer token auth; checks `auth_tokens` table in SQLite, falls back to `BOOKSHELF_AUTH_TOKEN` env var
- `api/notes.py` — Notes CRUD endpoints (`GET`/`POST`/`PUT`/`DELETE` on `/api/books/{id}/notes`)
- `api/capture.py` — Capture triage endpoints (`GET`/`PUT` on `/api/capture`, `POST /api/capture/{id}/apply`, `POST /api/capture/{id}/discard`)
- `api/telegram_bot.py` — Long-polling Telegram bot that inserts inbound messages into the `capture_events` table; runs as a separate systemd service (`bookshelf-telegram-bot`) on the VPS
- `api/google_books.py` — Google Books API search for the lookup endpoint
- `api/sync.py` — Goodreads RSS sync (deprecated, returns 410)
- `site/index.html` — single-file frontend (vanilla JS + embedded CSS); fetches `/api/*` endpoints; clicking a book navigates to its detail page
- `site/book.html` — individual book detail page with notes display and inline note add/edit/delete (auth required for writes)
- `site/add.html` — add book form with Google Books lookup
- `site/edit.html` — edit book form with delete
- `site/inbox.html` — triage UI for pending captures; each card lets you pick a book, note type, content, page, and tags, then apply (creates a note) or discard

**Mobile capture pipeline:**
1. User texts a Telegram bot from their phone → `api/telegram_bot.py` validates the sender against `TELEGRAM_ALLOWED_CHAT_ID` and inserts the raw message into `capture_events` with `status='pending'`.
2. User opens `/inbox` on the site, which lists pending captures via `GET /api/capture`.
3. For each capture, the user fills in resolved fields (book, note_type, content, page, tags) and hits Apply. The frontend calls `PUT /api/capture/{id}` to persist the resolution, then `POST /api/capture/{id}/apply` to materialize a note and an `activity_log` entry. The note's `created_at` is backdated to the capture's original timestamp.
4. Unwanted captures are dismissed via `POST /api/capture/{id}/discard`.
5. The `capture_events` table is created by migration v8 in `db.py` and auto-runs on API startup; `make add-capture-table` / `scripts/add_capture_table.py` exist for legacy one-shot bootstrap but are not needed for normal deploys.

**Shared utilities (`bookshelf_data.py`):**
- `load_env_file()` — custom `.env` parser (no python-dotenv dependency)
- `JsonFileCache` — mtime-aware JSON cache with deepcopy isolation
- `BookshelfStore` — high-level interface to JSON data files (legacy)
- `BookshelfDB` — SQLite-backed replacement for `BookshelfStore`; returns the same dict structures so API and frontend work unchanged
- `compute_books_hash()` — SHA-256 of read shelf (title, author, rating, review) used for LLM cache invalidation

## Deploy workflow

**Code and data are decoupled.** The VPS database is canonical — deploys only sync code, never overwrite the DB.

**Routine deploy:**
1. Make code changes locally (including new migrations in `db.py` if needed)
2. `make deploy-staging` → rsync code → restart → migrations auto-run on startup
3. Verify on `dev.book.tanxy.net`
4. `make deploy` → backup VPS DB → rsync code → restart API → restart telegram bot (if installed) → migrations auto-run

`make deploy` chains `backup → deploy-sync → restart-api → restart-bot`. The `restart-bot` step is idempotent: it checks `systemctl list-unit-files` and silently skips if `bookshelf-telegram-bot.service` is not installed, so deploys stay green before the first-time bot install.

**First-time Telegram bot install (VPS, one-time per host):**

The telegram bot runs as a separate systemd service from the API. The code and unit file are rsynced by `make deploy-sync`, but the package install, env vars, and `systemctl enable` must be done by hand once:

1. Ensure `python-telegram-bot>=21.0` is importable by the VPS venv:
   ```bash
   ssh root@134.199.239.64 '/var/www/book.tanxy.net/.venv/bin/pip install "python-telegram-bot>=21.0"'
   ```
2. Add the bot credentials to `/etc/bookshelf.env` (or whatever `EnvironmentFile` the unit points at):
   ```
   TELEGRAM_BOT_TOKEN=<from @BotFather>
   TELEGRAM_ALLOWED_CHAT_ID=<your personal chat id>
   ```
3. Install the systemd unit (rsynced to `$VPS_PATH/deploy/telegram-bot.service` by `make deploy-sync`):
   ```bash
   ssh root@134.199.239.64 '
     cp /var/www/book.tanxy.net/deploy/telegram-bot.service /etc/systemd/system/bookshelf-telegram-bot.service &&
     systemctl daemon-reload &&
     systemctl enable --now bookshelf-telegram-bot
   '
   ```
4. Verify: `make bot-status` → should show `active (running)`. Send a test message to the bot and check `make bot-logs`.
5. After the first install, subsequent `make deploy` runs pick the bot up automatically via `restart-bot`.

Staging currently has no bot service — captures only come in against production. If you want a staging bot, repeat the steps above against `STAGING_VPS_PATH` with a second `TELEGRAM_BOT_TOKEN`.

**If a migration fails on startup:**
1. API won't start, but `make backup` already saved the pre-migration DB
2. Rollback: revert code commit, redeploy, restore backup DB
3. Staging catches this first — always deploy there before prod

**Working with data locally:**
- `make pull-db` to get the latest production data
- `make push-db` to overwrite VPS data (asks for confirmation)
- `make seed-staging` to copy prod DB to staging on VPS

## Environment Variables

Loaded automatically from `.env` at repo root via `load_env_file()`. See `.env.example`.

| Variable | Default | Notes |
|----------|---------|-------|
| `ANTHROPIC_API_KEY` | — | Required for taste profile + recommendations |
| `OPENAI_API_KEY` | — | Required for GPT recommendations |
| `GEMINI_API_KEY` | — | Required for Gemini recommendations |
| `ANTHROPIC_MODEL` | `claude-opus-4-20250514` | |
| `OPENAI_MODEL` | `gpt-4.1` | |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | |
| `DB_PATH` | — | Path to SQLite DB; enables SQLite backend when set and file exists |
| `BOOKS_DATA` | `data/books.json` | JSON fallback when `DB_PATH` is unset |
| `LLM_CACHE_DATA` | `data/llm_cache.json` | JSON fallback when `DB_PATH` is unset |
| `GOODREADS_USER_ID` | — | Required for `POST /api/sync` |
| `BOOKSHELF_AUTH_TOKEN` | — | Bearer token for write endpoints; also checked against `auth_tokens` table in SQLite |
| `BOOKSHELF_CORS_ORIGINS` | — | Comma-separated allowed origins |
| `TELEGRAM_BOT_TOKEN` | — | Required for `api/telegram_bot.py`; obtained from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | — | Required for `api/telegram_bot.py`; numeric chat ID allowed to send captures — all other senders are ignored |

## API Endpoints

```
GET    /api/books              # All shelves + stats (includes note_count per book)
GET    /api/taste-profile      # Anthropic-generated reading taste analysis
GET    /api/recommendations    # Anthropic + OpenAI + Gemini book recommendations
GET    /api/health             # Server status + data availability flags + data_backend (sqlite/json)
GET    /api/lookup?q=...       # Google Books metadata search
GET    /api/llm-status         # LLM regeneration status (idle/running)
GET    /api/books/{id}/notes   # All notes for a book (public)
POST   /api/books              # Add a book (auth required, SQLite only)
PUT    /api/books/{id}         # Update a book (auth required, SQLite only)
DELETE /api/books/{id}         # Delete a book (auth required, SQLite only)
POST   /api/books/{id}/notes   # Add a note (auth required)
PUT    /api/books/{id}/notes/{note_id}    # Update a note (auth required)
DELETE /api/books/{id}/notes/{note_id}    # Delete a note (auth required)
GET    /api/capture            # List capture events (auth required; ?status=pending|applied|discarded, default pending)
PUT    /api/capture/{id}       # Update resolved_* fields on a pending capture (auth required)
POST   /api/capture/{id}/apply # Materialize capture as a note + activity_log entry (auth required)
POST   /api/capture/{id}/discard # Mark capture as discarded (auth required)
POST   /api/llm/regenerate     # Trigger async LLM regeneration (auth required)
POST   /api/sync               # Deprecated (returns 410 Gone)
```

## Key Patterns

- **LLM outputs** have strict JSON schemas enforced by `normalize_taste_profile()` and `normalize_recommendations()`. Partial failures are cached with an `"error"` key so one provider failing doesn't block the other.
- **Partial reruns**: `scripts/generate_llm.py --provider gemini` refreshes only that recommendation column and preserves the rest of the cache. Add `--with-taste-profile` to also rerun the taste profile; use `--taste-profile-provider openai` to generate it with OpenAI instead of Claude.
- **Rate limit retries**: `with_retry()` in `generate_llm.py` does up to 3 attempts; on 429 it reads the `retry-after` response header (falls back to 60s). Two Anthropic calls are made per build (taste profile, then recommendations), so rate limits are expected with Opus.
- **Title cleaning**: Goodreads titles like `"Dune (Dune #1)"` are stripped to `"Dune"` during CSV parse.
- **Auth**: Write endpoints require `Authorization: Bearer <token>`. Tokens are stored as SHA-256 hashes in the `auth_tokens` table. The env var `BOOKSHELF_AUTH_TOKEN` is a fallback when no SQLite DB is available.
- **LLM regeneration**: Write operations on the "read" shelf automatically trigger async LLM regeneration. An `asyncio.Lock` prevents concurrent runs.
- **Notes**: Per-book notes stored in the `notes` table with types: `thought`, `quote`, `connection`, `disagreement`, `question`. Tags stored as JSON array strings. Connection notes can link to another book via `connected_source_id`. The `source_type` field is always `'book'` for now (future-proofed for other content types). Notes do NOT trigger LLM regeneration.
- **Mobile capture**: Captures live in `capture_events` with lifecycle `pending → applied | discarded`. Pending rows only hold `raw_text` + `source_channel`. Applying a capture writes the resolved fields, creates a note, and creates an `activity_log` entry with `event_type='note_added'`. The note's `created_at` is backdated to the capture's original `created_at` so the activity feed reflects *when the thought happened*, not when it was triaged. Double-apply returns 400.
