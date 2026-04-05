# Xinyu's Bookshelf

Personal reading site for [book.tanxy.net](https://book.tanxy.net): a self-owned library archive, reading notebook, and lightweight public website for showing not just what has been read, but how that reading life is changing over time.

## Motivation

This project started as a way to keep full ownership over reading data that would otherwise live inside Goodreads exports, scattered notes, and one-off recommendation experiments.

The goal is not to build a generic "books app." The goal is to make one reader's intellectual life legible:

- what has been read, is being read, and is waiting on the shelf
- what patterns show up across years, genres, and geographies
- what thoughts, quotes, disagreements, and connections a book produces
- what has changed recently, so the site feels actively maintained rather than frozen

In short: this is a personal reading archive with public presentation, not a social network or a mass-market tracker.

## Scope

### In scope

- A public website for browsing the library
- A private write surface for adding/editing books and notes
- A canonical SQLite database owned by the site
- Typed per-book notes, including internal book-to-book connections and external references
- A small public activity log showing recent changes on the site
- Build-time LLM features such as taste profile and recommendation columns
- A simple staging/production deploy workflow on a VPS

### Out of scope

- Multi-user accounts
- Social features, comments, likes, follows, or feeds
- Real-time collaboration
- General-purpose CMS functionality
- Treating Goodreads as a live source of truth after import

## What the site does

### Public experience

- Homepage with:
  - `Reading Life` week-grid visualization
  - `Taste Profile`
  - `Books Read`, `Reading`, `Want to read`, and AI recommendation sections
  - a compact activity band that shows recent changes
- Individual book pages with review text and typed notes
- A public `/log` page with paginated recent activity

### Private/editor experience

- Add, edit, and delete books directly through the site
- Add, edit, and delete notes directly on book pages
- Create typed notes:
  - thought
  - quote
  - connection
  - disagreement
  - question
- For connection notes:
  - link to another book already in the bookshelf
  - or reference an external work with a label and optional URL

### Data + enrichment

- Goodreads CSV import for initial migration
- SQLite-backed canonical data store
- Legacy JSON fallback mode when SQLite is not configured
- LLM-generated taste profile
- LLM-generated recommendation columns from Anthropic, OpenAI, and Gemini
- Activity log entries for milestone events such as adding books, starting/finishing books, and adding notes

## Architecture

- Backend: FastAPI
- Database: SQLite
- Frontend: static HTML/CSS/JS pages served directly
- Deployment: single VPS with separate staging and production services
- AI enrichment: pre-generated cache stored in SQLite or JSON fallback

The app is intentionally small and direct. There is no ORM, no frontend framework, and no separate admin product. Most of the project complexity lives in keeping the reading experience elegant while preserving simple operational primitives.

## Project structure

```text
bookshelf/
├── api/
│   ├── main.py                # FastAPI app (books, health, lookup, LLM endpoints)
│   ├── activity.py            # Public activity feed endpoint
│   ├── auth.py                # Bearer-token auth
│   ├── notes.py               # Notes CRUD endpoints
│   ├── google_books.py        # Google Books lookup
│   ├── sync.py                # Legacy Goodreads RSS sync helpers
│   └── requirements.txt
├── data/
│   ├── bookshelf.db           # Local SQLite database (gitignored)
│   ├── books.json             # Legacy JSON fallback
│   ├── llm_cache.json         # Legacy JSON fallback
│   └── goodreads_library_export.csv
├── deploy/
│   ├── bookshelf.service
│   ├── bookshelf-staging.service
│   ├── nginx.conf
│   ├── nginx.staging.conf
│   ├── nginx.bootstrap.conf
│   ├── nginx.staging.bootstrap.conf
│   └── staging.env.example
├── scripts/
│   ├── add_notes_table.py     # Legacy helper for notes schema bootstrapping
│   ├── generate_llm.py
│   ├── migrate_json_to_sqlite.py
│   ├── parse_goodreads.py
│   └── prompts/
├── site/
│   ├── index.html             # Homepage
│   ├── book.html              # Book detail page
│   ├── log.html               # Public activity log page
│   ├── add.html               # Add book form
│   ├── edit.html              # Edit book form
│   └── data/                  # Local static data during development
├── bookshelf_data.py          # Storage abstraction (SQLite or JSON fallback)
├── db.py                      # Schema, migrations, and DB helpers
├── Makefile
└── README.md
```

## Environment

Copy `.env.example` to `.env` and fill in the values you need. The local API loads `.env` automatically from the repo root.

Required for full LLM generation:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

Common optional settings:

- `DB_PATH` — path to the SQLite database; enables SQLite mode when the file exists
- `BOOKSHELF_AUTH_TOKEN` — bearer token for write endpoints
- `BOOKS_DATA` — JSON fallback path when SQLite is not configured
- `LLM_CACHE_DATA` — JSON fallback cache path
- `BOOKSHELF_CORS_ORIGINS`
- `ENVIRONMENT`
- `LLM_DRY_RUN`
- `ANTHROPIC_MODEL`
- `OPENAI_MODEL`
- `GEMINI_MODEL`

## Local development

```bash
cp .env.example .env
make install
```

### First-time setup from Goodreads export

Place your export at `data/goodreads_library_export.csv`, then run:

```bash
make build
python scripts/migrate_json_to_sqlite.py --db data/bookshelf.db
```

Then set:

```bash
DB_PATH=data/bookshelf.db
BOOKSHELF_AUTH_TOKEN=<generated token>
```

### Run locally

```bash
make dev
```

Default local URLs:

- Site: `http://localhost:8000`
- API: `http://127.0.0.1:8001`

If those ports are occupied, you can run the site/API manually on alternate ports.

## Common commands

```bash
make install               # create .venv and install backend dependencies
make dev                   # run API + static site locally
make parse                 # Goodreads CSV -> books.json
make llm                   # generate/update LLM cache from books.json
make llm-force             # force regenerate LLM cache
make build                 # parse + llm
make add-notes-table       # legacy helper for notes schema creation
```

Useful direct commands:

```bash
./.venv/bin/python scripts/migrate_json_to_sqlite.py --db data/bookshelf.db
./.venv/bin/python scripts/generate_llm.py --db data/bookshelf.db --provider gemini
./.venv/bin/python scripts/generate_llm.py --db data/bookshelf.db --provider gemini --with-taste-profile
```

## Data model notes

### Books

Books are stored with shelf state, ratings, dates, review text, cover metadata, and auxiliary shelf tags.

### Notes

Notes are first-class records attached to a book. They are ordered newest-first on the book page.

Connection notes support two shapes:

- internal connection
  - `connected_source_id` points to another book in the bookshelf
- external connection
  - `connected_label` stores the referenced work
  - `connected_url` optionally stores an outbound link

### Activity log

The site keeps an append-only activity log for public freshness. It currently records milestone events:

- book added to `to_read`
- book moved to `currently_reading`
- book moved to `read`
- note added

This drives both the homepage activity band and the `/log` page.

## API

Public read endpoints:

```text
GET    /api/books
GET    /api/activity
GET    /api/taste-profile
GET    /api/recommendations
GET    /api/health
GET    /api/lookup?q=...
GET    /api/llm-status
GET    /api/books/{id}/notes
```

Authenticated write endpoints:

```text
POST   /api/books
PUT    /api/books/{id}
DELETE /api/books/{id}
POST   /api/books/{id}/notes
PUT    /api/books/{id}/notes/{note_id}
DELETE /api/books/{id}/notes/{note_id}
POST   /api/llm/regenerate
```

Deprecated:

```text
POST   /api/sync
```

All write endpoints require:

```text
Authorization: Bearer <token>
```

Write operations affecting the `read` shelf trigger asynchronous LLM regeneration.

## Deploy

Code and data are intentionally decoupled:

- the VPS SQLite database is the source of truth
- deploys sync code, not library data
- schema migrations run automatically when the API opens the database

### Standard flow

```bash
make deploy-staging
# verify on https://dev.book.tanxy.net

make deploy
```

### Deploy commands

```bash
make deploy                # backup prod DB -> sync code -> restart prod API
make deploy-staging        # sync code -> restart staging API
make backup                # snapshot production DB on the VPS
make restart-api
make restart-staging-api
```

### Database sync commands

```bash
make pull-db               # download production DB to local
make push-db               # upload local DB to production (destructive)
make seed-staging          # copy production DB to staging on the VPS
```

### Operational note

In practice, be careful with full-site syncs if you have local-only `site/data/` or other untracked development artifacts. The canonical reading data should always be treated as the VPS database, not as whatever happens to be in a local static folder.

## Staging

Staging lives beside production on the same VPS:

| | Production | Staging |
|---|---|---|
| Domain | `book.tanxy.net` | `dev.book.tanxy.net` |
| App root | `/var/www/book.tanxy.net` | `/var/www/dev.book.tanxy.net` |
| API port | `8001` | `8002` |
| Service | `bookshelf-api` | `bookshelf-staging` |
| Database | `data/bookshelf.db` | `data/bookshelf-staging.db` |

Typical workflow:

```bash
make deploy-staging
make seed-staging          # optional, if you want current production data
make restart-staging-api
```

## LLM cache behavior

`scripts/generate_llm.py` computes a content hash from the read shelf. If the hash has not changed, generation is skipped unless `--force` is used.

- Anthropic powers the taste profile and one recommendation column
- OpenAI powers one recommendation column
- Gemini powers one recommendation column
- partial refreshes are supported by provider
- `LLM_DRY_RUN=true` writes placeholders instead of making live model calls

If one provider fails, the other cached outputs can still be retained and displayed.

## Design philosophy

This codebase intentionally favors:

- directness over framework sprawl
- ownership over platform dependence
- editorial presentation over dashboard clutter
- readable data and notes over engagement mechanics

The bookshelf should feel like a real reading practice made visible, not like a generic app shell with books dropped into it.
