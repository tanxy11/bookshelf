# Xinyu's Bookshelf

Personal reading site for [book.tanxy.net](https://book.tanxy.net), built from a Goodreads CSV export and enriched with pre-generated LLM features.

## What it does

- Stores book data in a self-owned SQLite database (migrated from Goodreads CSV)
- Generates a build-time taste profile from the read shelf
- Generates side-by-side AI Picks (recommendations) from Anthropic and OpenAI, using reviews as primary signal; can surface books already on the to-read shelf
- Serves everything through a small FastAPI backend
- Renders a single-file frontend with search, filters, sort controls, and expandable book cards

## Project structure

```text
bookshelf/
├── api/
│   ├── main.py
│   └── requirements.txt
├── data/
│   ├── bookshelf.db              # SQLite database (canonical, gitignored)
│   ├── goodreads_library_export.csv
│   ├── books.json                # Legacy JSON (fallback)
│   └── llm_cache.json            # Legacy JSON (fallback)
├── deploy/
│   ├── bookshelf.service
│   └── nginx.conf
├── scripts/
│   ├── generate_llm.py
│   ├── migrate_json_to_sqlite.py # One-time migration
│   └── parse_goodreads.py
├── site/
│   └── index.html
├── bookshelf_data.py
├── db.py                         # SQLite schema + migrations
└── Makefile
```

## Environment

Copy `.env.example` to `.env` and fill in your keys. The local API and LLM generator now load `.env` automatically from the repo root, so you do not need to `export` the variables by hand for normal local runs.

Required:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

Optional:

- `DB_PATH` — path to SQLite database; enables SQLite backend when set and file exists
- `ANTHROPIC_MODEL`
- `OPENAI_MODEL`
- `LLM_DRY_RUN`
- `ENVIRONMENT`
- `BOOKSHELF_AUTH_TOKEN` — bearer token for write endpoints
- `BOOKS_DATA` — JSON fallback when `DB_PATH` is unset
- `LLM_CACHE_DATA` — JSON fallback when `DB_PATH` is unset
- `BOOKSHELF_CORS_ORIGINS`

`OPENAI_MODEL` defaults to `gpt-4.1` in this repo so the OpenAI side stays configurable even if older preview IDs are unavailable.

## Local development

Recommended local setup:

```bash
cp .env.example .env
make install
```

Then update `.env` with your real API keys, place your Goodreads export at `data/goodreads_library_export.csv`, and run:

```bash
make build
make dev
```

`make` will automatically use `.venv/bin/python` once `make install` has created it, so activating the virtualenv is optional.

That serves:

- Site: `http://localhost:8010`
- API: `http://127.0.0.1:8001`

## Build targets

```bash
make install        # create .venv and install api dependencies
make parse          # CSV -> data/books.json
make llm            # data/books.json -> data/llm_cache.json (skips if unchanged)
make llm-force      # always regenerate LLM outputs
make llm-staging    # staging cache with dry-run or cheap-model overrides
make llm-staging-force
make refresh-data   # parse + llm
make build          # alias for refresh-data
make build FORCE_LLM=1
make dev            # run FastAPI + static site locally
make deploy         # rsync current site/data/api/deploy assets to the VPS
make deploy-staging # rsync current site/data/api/deploy assets to the staging VPS path
```

## API

- `GET /api/books`
- `GET /api/taste-profile`
- `GET /api/recommendations` — "AI Picks": side-by-side Anthropic + OpenAI recommendations; includes `from_to_read: true` when a book comes from the to-read shelf
- `GET /api/health`

All API responses are read from SQLite (or JSON files as fallback). Visitors never trigger live LLM calls.

## Local validation checklist

```bash
cp .env.example .env
make install
make build
make dev
```

If `make build` succeeds, you should have:

- `data/books.json`
- `data/llm_cache.json`

If only one provider key is valid, the build still succeeds and caches the working side.

## LLM cache behavior

`scripts/generate_llm.py` computes a SHA-256 hash from the sorted read shelf using:

- title
- author
- my rating
- my review

If the hash matches `data/llm_cache.json`, generation is skipped unless `--force` is used.

Anthropic powers:

- Taste profile
- One side of the recommendations

OpenAI powers:

- The second side of the recommendations

If one recommendation provider fails, the other still gets cached and displayed.

If `LLM_DRY_RUN=true`, the generator skips live provider calls and writes placeholder content marked with `[DRY RUN]`. This is intended for staging so we can exercise the full deploy and UI flow without burning API credits.

## SQLite migration

The canonical data store is now SQLite. To migrate from JSON:

```bash
python scripts/migrate_json_to_sqlite.py --db data/bookshelf.db
```

Then set `DB_PATH=data/bookshelf.db` in your `.env`. The migration script generates an auth token — save it as `BOOKSHELF_AUTH_TOKEN` in `.env`.

The app auto-detects: if `DB_PATH` is set and the file exists, it uses SQLite. Otherwise it falls back to JSON files.

## Deployment notes

`make deploy` syncs code and data to the VPS. Once SQLite is active on the VPS, the database there is canonical — deploys should sync code only, not overwrite the VPS database.

After deploy, restart the service on the server if API code changed:

```bash
sudo systemctl restart bookshelf-api
```

## Staging

Staging is configured for `dev.book.tanxy.net` on the same VPS with its own app root, service, and API port:

- App root: `/var/www/dev.book.tanxy.net`
- API port: `8002`
- systemd unit: `bookshelf-staging`
- Nginx configs: `deploy/nginx.staging.conf` and `deploy/nginx.staging.bootstrap.conf`

By default, `make llm-staging` writes `data/llm_cache.staging.json` in `LLM_DRY_RUN=true` mode. If you want real staging LLM calls, set `STAGING_LLM_DRY_RUN=0`; the Makefile will use cheaper staging defaults:

- Anthropic: `claude-3-haiku-20240307`
- OpenAI: `gpt-4.1-nano`

Typical flow:

```bash
make llm-staging
make deploy-staging
```

Cloudflare/manual setup still needed:

- Add `dev.book.tanxy.net` pointing to the same VPS IP as production
- If the nested subdomain has TLS handshake problems while proxied through Cloudflare, switch `dev.book.tanxy.net` to `DNS only` until edge cert coverage is in place
- After DNS resolves, install a TLS cert for `dev.book.tanxy.net`
- Put the staging env file on the server at `/etc/bookshelf-staging.env`
