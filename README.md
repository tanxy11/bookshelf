# Xinyu's Bookshelf

Personal reading site for [book.tanxy.net](https://book.tanxy.net), built from a Goodreads CSV export and enriched with pre-generated LLM features.

## What it does

- Parses a Goodreads export into structured `books.json`
- Generates a build-time taste profile from the read shelf
- Generates side-by-side recommendation sets from Anthropic and OpenAI
- Serves everything through a small FastAPI backend
- Renders a single-file frontend with search, filters, sort controls, and expandable book cards

## Project structure

```text
bookshelf/
├── api/
│   ├── main.py
│   └── requirements.txt
├── data/
│   ├── goodreads_library_export.csv
│   ├── books.json
│   └── llm_cache.json
├── deploy/
│   ├── bookshelf.service
│   └── nginx.conf
├── scripts/
│   ├── generate_llm.py
│   └── parse_goodreads.py
├── site/
│   └── index.html
├── bookshelf_data.py
└── Makefile
```

## Environment

Copy `.env.example` to `.env` and fill in your keys. The local API and LLM generator now load `.env` automatically from the repo root, so you do not need to `export` the variables by hand for normal local runs.

Required:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

Optional:

- `ANTHROPIC_MODEL`
- `OPENAI_MODEL`
- `BOOKS_DATA`
- `LLM_CACHE_DATA`
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

- Site: `http://localhost:8000`
- API: `http://127.0.0.1:8001`

## Build targets

```bash
make install        # create .venv and install api dependencies
make parse          # CSV -> data/books.json
make llm            # data/books.json -> data/llm_cache.json (skips if unchanged)
make llm-force      # always regenerate LLM outputs
make build          # parse + llm
make build FORCE_LLM=1
make dev            # run FastAPI + static site locally
make deploy         # build + rsync site/data/api/deploy assets to the VPS
```

## API

- `GET /api/books`
- `GET /api/taste-profile`
- `GET /api/recommendations`
- `GET /api/health`

All API responses are read from disk-backed JSON files. Visitors never trigger live LLM calls.

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

## Deployment notes

`make deploy` syncs:

- `site/`
- `data/books.json`
- `data/llm_cache.json`
- `api/`
- `deploy/nginx.conf`
- `deploy/bookshelf.service`

After deploy, restart the service on the server if API code changed:

```bash
sudo systemctl restart bookshelf
```
