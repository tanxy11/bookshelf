# Xinyu's Bookshelf

Personal reading history website at [book.tanxy.net](https://book.tanxy.net), built from a Goodreads export and kept in sync automatically via RSS.

## What it does

- Displays 380+ books read as a dense cover wall — hover to see title, author, rating, and review snippets
- Filters by shelf/tag, rating, and free-text search; sortable by date, rating, title, author, or pages
- Currently reading and want-to-read sections
- Auto-syncs with Goodreads every 6 hours via RSS — mark a book read on Goodreads, it appears on the site within 6 hours

## Stack

| Layer | Tech |
|---|---|
| Frontend | Single HTML file, vanilla JS, no build step |
| Backend | FastAPI + uvicorn, systemd service |
| Data | `books.json` (generated from Goodreads CSV, updated by RSS sync) |
| Hosting | DigitalOcean VPS, Nginx, Let's Encrypt SSL |
| Covers | Open Library Covers API |

## Project structure

```
bookshelf/
├── scripts/
│   └── parse_goodreads.py   # Goodreads CSV → books.json
├── api/
│   ├── main.py              # FastAPI: GET /api/books, POST /api/sync
│   ├── sync.py              # RSS fetch + merge logic
│   └── requirements.txt
├── site/
│   └── index.html           # Single-page frontend
├── deploy/
│   ├── nginx.conf           # Nginx site config
│   └── bookshelf-api.service  # systemd service
└── Makefile                 # parse / dev / deploy / sync
```

## Local development

**Prerequisites:** Python 3.11+, a Goodreads CSV export

```bash
# Export your library from Goodreads:
# Settings → Import and Export → Export Library
# Save as data/goodreads_library_export.csv

make dev   # parses CSV → books.json, serves site at http://localhost:8000
```

## Deploying to a VPS

### First-time setup

```bash
# 1. Set your Goodreads user ID on the server
#    (find it at goodreads.com/user/show/XXXXXXXX)
ssh root@your-vps 'cat > /etc/bookshelf.env << EOF
GOODREADS_USER_ID=YOUR_ID
BOOKS_DATA=/var/www/book.tanxy.net/data/books.json
EOF'

# 2. Deploy everything
make deploy        # push frontend + initial books.json
make deploy-api    # install FastAPI, set up systemd
make deploy-nginx  # push nginx config

# 3. Seed data and verify
make sync          # first RSS sync
```

### Routine updates

```bash
make deploy   # push frontend changes
make sync     # force an immediate RSS sync
```

Auto-sync runs every 6 hours via cron on the VPS:
```
0 */6 * * * curl -s -X POST http://127.0.0.1:8001/api/sync >> /var/log/bookshelf-sync.log 2>&1
```

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/books` | Returns full `books.json` |
| `POST` | `/api/sync` | Fetches Goodreads RSS and merges new/updated books |
| `GET` | `/api/health` | Health check |

## Roadmap

- **v0.5** ✓ FastAPI backend, Goodreads RSS auto-sync
- **v1** SQLite database, per-book notes
- **v1.5** LLM-powered "recommend me a book" feature
