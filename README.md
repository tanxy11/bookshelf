# Xinyu's Bookshelf

Personal reading site for [book.tanxy.net](https://book.tanxy.net), built from a Goodreads CSV export and enriched with pre-generated LLM features.

## What it does

- Stores book data in a self-owned SQLite database (migrated from Goodreads CSV)
- Generates a build-time taste profile from the read shelf
- Generates three-way AI Picks (recommendations) from Anthropic, OpenAI, and Gemini, using reviews as primary signal; can surface books already on the to-read shelf
- Serves everything through a small FastAPI backend
- Renders a single-file frontend with search, filters, sort controls, and expandable book cards
- Supports adding and editing books directly through the site (auth required)

## Project structure

```text
bookshelf/
├── api/
│   ├── main.py               # FastAPI app (read + CRUD + LLM endpoints)
│   ├── auth.py                # Bearer token auth (SQLite + env var fallback)
│   ├── google_books.py        # Google Books API lookup
│   └── requirements.txt
├── data/
│   ├── bookshelf.db           # SQLite database (canonical, gitignored)
│   ├── backups/               # Timestamped DB backups (on VPS)
│   ├── goodreads_library_export.csv
│   ├── books.json             # Legacy JSON (fallback)
│   └── llm_cache.json         # Legacy JSON (fallback)
├── deploy/
│   ├── bookshelf.service
│   ├── bookshelf-staging.service
│   ├── nginx.conf
│   ├── nginx.staging.conf
│   └── staging.env.example
├── scripts/
│   ├── generate_llm.py
│   ├── migrate_json_to_sqlite.py
│   └── parse_goodreads.py
├── site/
│   ├── index.html             # Main frontend
│   ├── add.html               # Add book form
│   └── edit.html              # Edit book form
├── bookshelf_data.py
├── db.py                      # SQLite schema + migrations
└── Makefile
```

## Environment

Copy `.env.example` to `.env` and fill in your keys. The local API and LLM generator load `.env` automatically from the repo root.

Required:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

Optional:

- `DB_PATH` — path to SQLite database; enables SQLite backend when set and file exists
- `BOOKSHELF_AUTH_TOKEN` — bearer token for write endpoints
- `ANTHROPIC_MODEL`
- `OPENAI_MODEL`
- `GEMINI_MODEL`
- `LLM_DRY_RUN`
- `ENVIRONMENT`
- `BOOKS_DATA` — JSON fallback when `DB_PATH` is unset
- `LLM_CACHE_DATA` — JSON fallback when `DB_PATH` is unset
- `BOOKSHELF_CORS_ORIGINS`

## Local development

```bash
cp .env.example .env       # fill in API keys
make install               # create .venv, install deps
```

### First-time setup (from Goodreads export)

Place your CSV at `data/goodreads_library_export.csv`, then:

```bash
make build                 # parse CSV → books.json → llm_cache.json
python scripts/migrate_json_to_sqlite.py  # creates data/bookshelf.db + auth token
```

Add the generated auth token and `DB_PATH=data/bookshelf.db` to your `.env`.

### Running locally

```bash
make dev
```

That serves:

- Site: `http://localhost:8000`
- API: `http://127.0.0.1:8001`

`make` will automatically use `.venv/bin/python` once `make install` has created it, so activating the virtualenv is optional.

## Build targets

```bash
make install               # create .venv and install api dependencies
make dev                   # run FastAPI + static site locally
make parse                 # CSV → data/books.json (legacy)
make llm                   # data/books.json → data/llm_cache.json (legacy, skips if unchanged)
make llm-force             # always regenerate LLM outputs (legacy)
make build                 # parse + llm (legacy pipeline)
```

## Deploy

**Code and data are decoupled.** The VPS database is canonical — deploys only sync code, never overwrite the DB. Schema migrations run automatically on API startup.

### Routine workflow

```bash
# 1. Make code changes locally
# 2. Deploy to staging first
make deploy-staging        # rsync code → restart staging API

# 3. Verify on https://dev.book.tanxy.net

# 4. Deploy to production
make deploy                # backup VPS DB → rsync code → restart API
```

### Deploy targets

```bash
make deploy                # backup → rsync code → restart prod API
make deploy-staging        # rsync code → restart staging API
make backup                # snapshot VPS DB to data/backups/bookshelf-{timestamp}.db
```

### Database management

The VPS database is the source of truth. Use these to sync data between local and VPS:

```bash
make pull-db               # download VPS DB → local data/bookshelf.db
make push-db               # upload local DB → VPS (interactive confirmation)
make seed-staging          # copy prod DB → staging DB on VPS
```

### If a migration fails on startup

1. API won't start, but `make backup` already saved the pre-migration DB
2. Rollback: revert the code commit, redeploy, restore backup DB
3. Staging catches this first — always deploy there before prod

## API

```
GET    /api/books              # All shelves + stats
GET    /api/taste-profile      # Anthropic-generated reading taste analysis
GET    /api/recommendations    # Three-way Anthropic + OpenAI + Gemini recommendations
GET    /api/health             # Server status + data backend (sqlite/json)
GET    /api/lookup?q=...       # Google Books metadata search
GET    /api/llm-status         # LLM regeneration status (idle/running)
POST   /api/books              # Add a book (auth required)
PUT    /api/books/{id}         # Update a book (auth required)
DELETE /api/books/{id}         # Delete a book (auth required)
POST   /api/llm/regenerate     # Trigger async LLM regeneration (auth required)
```

All read endpoints are public. Write endpoints require `Authorization: Bearer <token>`.

Write operations on the "read" shelf automatically trigger async LLM regeneration.

## SQLite migration

To migrate from JSON files:

```bash
python scripts/migrate_json_to_sqlite.py --db data/bookshelf.db
```

Then set `DB_PATH=data/bookshelf.db` in your `.env`. The migration script generates an auth token — save it as `BOOKSHELF_AUTH_TOKEN` in `.env`.

The app auto-detects: if `DB_PATH` is set and the file exists, it uses SQLite. Otherwise it falls back to JSON files.

## LLM cache behavior

`scripts/generate_llm.py` computes a SHA-256 hash from the sorted read shelf using title, author, rating, and review. If the hash matches the cache, generation is skipped unless `--force` is used.

Anthropic powers the taste profile and one recommendation column. OpenAI and Gemini power the other two recommendation columns. If one provider fails, the others still get cached and displayed.

If `LLM_DRY_RUN=true`, the generator writes placeholder content marked with `[DRY RUN]` without making live API calls.

## Staging

Staging is configured for `dev.book.tanxy.net` on the same VPS:

| | Production | Staging |
|---|---|---|
| Domain | `book.tanxy.net` | `dev.book.tanxy.net` |
| App root | `/var/www/book.tanxy.net` | `/var/www/dev.book.tanxy.net` |
| API port | 8001 | 8002 |
| Service | `bookshelf-api` | `bookshelf-staging` |
| Database | `data/bookshelf.db` | `data/bookshelf.db` (own copy) |

Typical flow:

```bash
make deploy-staging        # deploy code to staging
make seed-staging          # optionally seed with prod data
make restart-staging-api   # restart after seeding
```

Cloudflare note: if `dev.book.tanxy.net` has TLS handshake problems while proxied, switch to `DNS only` until edge cert coverage is in place.
