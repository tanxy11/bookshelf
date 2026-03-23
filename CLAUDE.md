# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install         # Create .venv and install dependencies
make parse           # Goodreads CSV → data/books.json
make llm             # data/books.json → data/llm_cache.json (skips if books unchanged)
make llm-force       # Always regenerate LLM outputs
make build           # parse + llm
make build FORCE_LLM=1  # Force full rebuild
make dev             # Run FastAPI (port 8001) + static site (port 8000) locally
make deploy          # build + rsync to VPS over SSH
```

**Run tests** (stdlib unittest, no test runner configured):
```bash
.venv/bin/python -m pytest tests/
# or single file:
.venv/bin/python -m pytest tests/test_generate_llm.py
```

## Architecture

**Data pipeline (build-time):**
1. `scripts/parse_goodreads.py` — parses Goodreads CSV export → `data/books.json`
2. `scripts/generate_llm.py` — calls Anthropic + OpenAI APIs → `data/llm_cache.json` (skips if SHA-256 hash of read shelf is unchanged)

**Runtime:**
- `api/main.py` — FastAPI server; reads from `data/books.json` and `data/llm_cache.json` via `BookshelfStore`; serves pre-generated JSON
- `api/sync.py` — fetches Goodreads RSS feeds and merges updates into `data/books.json`; deduplicates by normalized title+author
- `site/index.html` — single-file frontend (vanilla JS + embedded CSS); fetches `/api/*` endpoints

**Shared utilities (`bookshelf_data.py`):**
- `load_env_file()` — custom `.env` parser (no python-dotenv dependency)
- `JsonFileCache` — mtime-aware JSON cache with deepcopy isolation
- `BookshelfStore` — high-level interface to both data files
- `compute_books_hash()` — SHA-256 of read shelf (title, author, rating, review) used for LLM cache invalidation

## Environment Variables

Loaded automatically from `.env` at repo root via `load_env_file()`. See `.env.example`.

| Variable | Default | Notes |
|----------|---------|-------|
| `ANTHROPIC_API_KEY` | — | Required for taste profile + recommendations |
| `OPENAI_API_KEY` | — | Required for GPT recommendations |
| `ANTHROPIC_MODEL` | `claude-opus-4-20250514` | |
| `OPENAI_MODEL` | `gpt-4.1` | |
| `BOOKS_DATA` | `data/books.json` | |
| `LLM_CACHE_DATA` | `data/llm_cache.json` | |
| `GOODREADS_USER_ID` | — | Required for `POST /api/sync` |

## API Endpoints

```
GET  /api/books            # All shelves + stats
GET  /api/taste-profile    # Anthropic-generated reading taste analysis
GET  /api/recommendations  # Anthropic + OpenAI book recommendations (side-by-side)
GET  /api/health           # Server status + data availability flags
POST /api/sync             # Fetch Goodreads RSS and merge into books.json
```

## Key Patterns

- **LLM outputs** have strict JSON schemas enforced by `normalize_taste_profile()` and `normalize_recommendations()`. Partial failures are cached with an `"error"` key so one provider failing doesn't block the other.
- **Rate limit retries**: `with_retry()` in `generate_llm.py` does up to 3 attempts; on 429 it reads the `retry-after` response header (falls back to 60s). Two Anthropic calls are made per build (taste profile, then recommendations), so rate limits are expected with Opus.
- **Title cleaning**: Goodreads titles like `"Dune (Dune #1)"` are stripped to `"Dune"` during CSV parse.
- **Frontend data**: `data/books.json` is also copied to `site/data/books.json` for offline/static serving.
