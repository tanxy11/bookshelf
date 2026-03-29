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
```

**Deploy:**
```bash
make deploy          # backup VPS DB → rsync code → restart API
make deploy-staging  # rsync code → restart staging API
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

**Data pipeline (build-time, legacy):**
1. `scripts/parse_goodreads.py` — parses Goodreads CSV export → `data/books.json`
2. `scripts/generate_llm.py` — calls Anthropic + OpenAI + Gemini APIs → `data/llm_cache.json` (skips if SHA-256 hash of read shelf is unchanged)

**Runtime:**
- `api/main.py` — FastAPI server; reads from SQLite via `BookshelfDB` (or JSON via `BookshelfStore` as fallback)
- `api/auth.py` — Bearer token auth; checks `auth_tokens` table in SQLite, falls back to `BOOKSHELF_AUTH_TOKEN` env var
- `api/google_books.py` — Google Books API search for the lookup endpoint
- `api/sync.py` — Goodreads RSS sync (deprecated, returns 410)
- `site/index.html` — single-file frontend (vanilla JS + embedded CSS); fetches `/api/*` endpoints
- `site/add.html` — add book form with Google Books lookup
- `site/edit.html` — edit book form with delete

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
4. `make deploy` → backup VPS DB → rsync code → restart → migrations auto-run

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

## API Endpoints

```
GET    /api/books              # All shelves + stats
GET    /api/taste-profile      # Anthropic-generated reading taste analysis
GET    /api/recommendations    # Anthropic + OpenAI + Gemini book recommendations
GET    /api/health             # Server status + data availability flags + data_backend (sqlite/json)
GET    /api/lookup?q=...       # Google Books metadata search
GET    /api/llm-status         # LLM regeneration status (idle/running)
POST   /api/books              # Add a book (auth required, SQLite only)
PUT    /api/books/{id}         # Update a book (auth required, SQLite only)
DELETE /api/books/{id}         # Delete a book (auth required, SQLite only)
POST   /api/llm/regenerate     # Trigger async LLM regeneration (auth required)
POST   /api/sync               # Deprecated (returns 410 Gone)
```

## Key Patterns

- **LLM outputs** have strict JSON schemas enforced by `normalize_taste_profile()` and `normalize_recommendations()`. Partial failures are cached with an `"error"` key so one provider failing doesn't block the other.
- **Rate limit retries**: `with_retry()` in `generate_llm.py` does up to 3 attempts; on 429 it reads the `retry-after` response header (falls back to 60s). Two Anthropic calls are made per build (taste profile, then recommendations), so rate limits are expected with Opus.
- **Title cleaning**: Goodreads titles like `"Dune (Dune #1)"` are stripped to `"Dune"` during CSV parse.
- **Auth**: Write endpoints require `Authorization: Bearer <token>`. Tokens are stored as SHA-256 hashes in the `auth_tokens` table. The env var `BOOKSHELF_AUTH_TOKEN` is a fallback when no SQLite DB is available.
- **LLM regeneration**: Write operations on the "read" shelf automatically trigger async LLM regeneration. An `asyncio.Lock` prevents concurrent runs.
