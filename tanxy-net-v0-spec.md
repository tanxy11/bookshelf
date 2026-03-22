# tanxy.net — Personal Book Shelf & Reading Hub

## Spec v0 — For Claude Code Implementation

---

## 1. Project Overview

A personal website hosted at **tanxy.net** that displays my reading history, lets visitors browse and filter my books, and (in v0.5) recommends books using an LLM. The data source is a Goodreads CSV export.

This will run on a VPS (Ubuntu) behind Nginx. The architecture should be designed so that a database backend (SQLite) and API server (FastAPI) can be added incrementally without rewriting the frontend.

---

## 2. V0 Scope

**In scope:**
- Parse Goodreads CSV export into a structured JSON data file
- Serve a single-page frontend that displays the book list
- Filtering and sorting UI
- Clean, public-facing design
- Nginx serving static files on the VPS

**Explicitly out of scope (future versions):**
- LLM-powered book recommendations (v0.5)
- Per-book notes / annotations (v1)
- SQLite database (v1)
- FastAPI backend (v0.5)
- User authentication
- Mobile app

---

## 3. Data Pipeline

### 3.1 Input
Goodreads CSV export (`goodreads_library_export.csv`). Key fields to extract:

| Goodreads CSV Column       | Internal Field       | Type     | Notes                              |
|----------------------------|----------------------|----------|------------------------------------|
| Title                      | `title`              | string   | Strip any series info in parens    |
| Author                     | `author`             | string   | Primary author                     |
| ISBN13                     | `isbn13`             | string   | For cover image lookup             |
| My Rating                  | `my_rating`          | int 0-5  | 0 means unrated                    |
| Average Rating             | `avg_rating`         | float    | Goodreads community average        |
| Number of Pages            | `pages`              | int      | Nullable                           |
| Date Read                  | `date_read`          | string   | YYYY-MM-DD or empty                |
| Date Added                 | `date_added`         | string   | YYYY-MM-DD                         |
| Bookshelves                | `shelves`            | string[] | Split on comma                     |
| Exclusive Shelf            | `exclusive_shelf`    | string   | "read", "to-read", "currently-reading" |
| My Review                  | `my_review`          | string   | Nullable, preserve if present      |

### 3.2 Processing Script

`scripts/parse_goodreads.py`

- Reads the CSV, maps columns to internal fields.
- Splits books into three collections by `exclusive_shelf`: `read`, `currently-reading`, `to-read`.
- Sorts `read` books by `date_read` descending (most recent first). Books with no `date_read` go to the end.
- Outputs `data/books.json` with structure:

```json
{
  "generated_at": "2026-03-21T12:00:00Z",
  "books": {
    "read": [ ... ],
    "currently_reading": [ ... ],
    "to_read": [ ... ]
  },
  "stats": {
    "total_read": 142,
    "total_to_read": 38,
    "avg_my_rating": 3.8,
    "books_this_year": 12,
    "top_authors": [
      { "author": "Author Name", "count": 5 }
    ]
  }
}
```

### 3.3 Book Cover Images

Use Open Library Covers API: `https://covers.openlibrary.org/b/isbn/{isbn13}-M.jpg`

Do NOT download/cache covers in v0. The frontend should use these URLs directly with a fallback placeholder for missing covers. Use a simple SVG or CSS placeholder showing the book's first letter.

---

## 4. Frontend

### 4.1 Tech Stack
- Single HTML file with embedded CSS and JS (no build step, no framework)
- Fetches `data/books.json` at load time
- Vanilla JS only — no React, no dependencies
- Responsive (works on mobile)

### 4.2 Design Direction
- Clean, minimal, slightly editorial feel. Think personal library, not Goodreads clone.
- Light background, good typography (use system font stack or a single Google Font like Inter or Source Serif).
- The site should feel like a person's curated shelf, not a database dump.
- Color palette: muted, warm. No bright colors. Let the book covers provide the color.

### 4.3 Page Structure

**Header:**
- Site title: "Xinyu's Bookshelf" (or similar — can be configured)
- Subtitle: one-liner like "What I've been reading"
- Simple nav: just anchor links to sections on the same page

**Stats Bar:**
- A compact row showing: total books read, books this year, average rating, currently reading count
- Not flashy — just informative

**Currently Reading Section:**
- If any books in `currently_reading`, show them prominently at the top
- Card layout: cover image, title, author, pages

**Read Books Section (main content):**
- Default view: grid of book cards (cover, title, author, my rating as stars, date read)
- Each card is clickable — expands inline or opens a detail panel showing: pages, avg rating, shelves, my review (if present)

**Controls Bar (above the grid):**
- **Sort by**: Date read (default) | My rating | Title (A-Z) | Author (A-Z) | Pages
- **Filter by shelf/tag**: Clickable shelf tags derived from `shelves` field. Clicking a tag filters to books with that shelf. Clicking again removes filter. Multiple filters = OR logic.
- **Search**: Simple text search across title + author
- **Rating filter**: "Show only: ★★★★★ | ★★★★+ | ★★★+ | All"

**To-Read Section:**
- Collapsed by default (expandable)
- Simple list view: title, author
- No ratings (obviously)

**Footer:**
- "Data from Goodreads, last updated {generated_at}"
- Link to GitHub repo (optional)

### 4.4 Interactions
- All filtering/sorting is client-side JS operating on the loaded JSON
- URL hash should update on filter/sort changes so links are shareable (e.g. `tanxy.net/#shelf=sci-fi&sort=rating`)
- Smooth transitions on filter changes (simple CSS transitions, nothing heavy)

---

## 5. Project Structure

```
tanxy-net/
├── scripts/
│   └── parse_goodreads.py       # CSV → JSON pipeline
├── data/
│   ├── goodreads_library_export.csv   # Raw input (gitignored)
│   └── books.json               # Generated output (gitignored)
├── site/
│   ├── index.html               # Single-page frontend
│   └── data/
│       └── books.json           # Copied here for serving
├── deploy/
│   └── nginx.conf               # Nginx site config
├── README.md
└── Makefile                     # Convenience commands
```

### 5.1 Makefile Targets

```makefile
parse:       # Run parse_goodreads.py, output to data/books.json, copy to site/data/
dev:         # Serve site/ locally on port 8000 (python -m http.server)
deploy:      # rsync site/ to VPS, reload nginx
```

---

## 6. Deployment

### 6.1 VPS Setup (manual, not in Claude Code scope)
- Ubuntu VPS with Nginx installed
- Domain tanxy.net pointed to VPS IP
- SSL via Let's Encrypt / certbot

### 6.2 Nginx Config
- Serve `site/` directory as static files
- Root at `/var/www/tanxy-net/`
- Enable gzip
- Cache headers for static assets
- Provide the nginx config file but do NOT attempt to run nginx commands

### 6.3 Deploy Flow
`make deploy` should:
1. Run `make parse`
2. `rsync` the `site/` directory to the VPS path
3. Print a message reminding to reload nginx if config changed

The Makefile should have a configurable `VPS_HOST` and `VPS_PATH` variable at the top.

---

## 7. Design Constraints & Principles

1. **No build step for the frontend.** The HTML/CSS/JS should work as-is when served by any static file server. No webpack, no npm, no transpilation.
2. **Data file is the source of truth.** The frontend is purely a renderer. All data logic lives in the Python parse script.
3. **Graceful degradation.** Missing covers, missing dates, missing ratings — all should render cleanly with appropriate fallbacks, never break the layout.
4. **Prepare for v0.5.** The JSON structure and frontend architecture should make it straightforward to later: (a) swap the static JSON fetch for an API call, (b) add a "Recommend me a book" button that POSTs to a backend endpoint.
5. **No over-engineering.** No database, no backend server, no auth, no admin panel. Those come later.

---

## 8. Future Versions (context only — do NOT implement)

These are documented so architectural decisions in v0 don't block them:

- **v0.5**: FastAPI server replaces static file serving for data. Add `/api/recommend` endpoint that sends book list + ratings to Claude and returns recommendations. Frontend gets a "Recommend" button.
- **v1**: SQLite database. Parse script writes to DB instead of JSON. FastAPI serves from DB. Add per-book notes (editable via a simple admin interface or API).
- **v1.5**: Full-text search across notes (SQLite FTS5). Cross-book connection discovery. This may integrate with the separate "personal knowledge management" project.

---

## 9. Acceptance Criteria

v0 is done when:

1. `make parse` reads a Goodreads CSV and produces a valid `books.json`
2. Opening `site/index.html` in a browser displays the book grid with covers, titles, authors, and ratings
3. Sort, filter, and search controls work correctly
4. Currently-reading and to-read sections render correctly
5. The page is responsive and looks good on mobile
6. The page looks like something I'd want to share publicly — not a raw data dump
7. `make deploy` rsyncs to a configurable VPS target
8. Nginx config is provided and serves the site correctly
