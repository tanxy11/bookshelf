"""
Fetch Goodreads RSS feeds and merge new/updated books into books.json.

Goodreads RSS URL:
  https://www.goodreads.com/review/list_rss/{user_id}?shelf={shelf}&per_page=200
"""

import json
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

SHELF_URL = "https://www.goodreads.com/review/list_rss/{user_id}?shelf={shelf}&per_page=200"

# Maps RSS shelf name → JSON collection key
SHELF_MAP = {
    "read":              "read",
    "currently-reading": "currently_reading",
    "to-read":           "to_read",
}


# ── RSS parsing ────────────────────────────────────────────────────────────────

def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_date(raw: str) -> str:
    """Convert various Goodreads date strings → YYYY-MM-DD or empty."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in (
        "%a %b %d %H:%M:%S %z %Y",   # "Fri Jan 01 00:00:00 -0800 2016"
        "%Y/%m/%d",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _parse_item(item: ET.Element, exclusive_shelf: str) -> dict:
    # pages is nested inside <book><num_pages>
    pages = None
    book_el = item.find("book")
    if book_el is not None:
        np = book_el.find("num_pages")
        if np is not None and (np.text or "").strip():
            try:
                pages = int(np.text.strip())
            except ValueError:
                pass

    isbn = _text(item, "isbn").strip('="')
    isbn13 = _text(item, "isbn13").strip('="')

    try:
        my_rating = int(_text(item, "user_rating") or "0")
    except ValueError:
        my_rating = 0

    try:
        avg_rating = round(float(_text(item, "average_rating") or "0"), 2)
    except ValueError:
        avg_rating = None

    shelves_raw = _text(item, "user_shelves")
    shelves = [s.strip() for s in shelves_raw.split(",") if s.strip()]

    review = _text(item, "user_review") or None

    return {
        "goodreads_id": _text(item, "book_id"),
        "title":         _text(item, "title"),
        "author":        _text(item, "author_name"),
        "isbn13":        isbn13 or isbn,
        "my_rating":     my_rating,
        "avg_rating":    avg_rating,
        "pages":         pages,
        "date_read":     _parse_date(_text(item, "user_read_at")),
        "date_added":    _parse_date(_text(item, "user_date_added")),
        "shelves":       shelves,
        "exclusive_shelf": exclusive_shelf,
        "my_review":     review,
    }


# ── Fetch ──────────────────────────────────────────────────────────────────────

async def _fetch_shelf(
    client: httpx.AsyncClient, user_id: str, rss_shelf: str, exclusive_shelf: str
) -> list[dict]:
    url = SHELF_URL.format(user_id=user_id, shelf=rss_shelf)
    try:
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return []
        return [_parse_item(item, exclusive_shelf) for item in channel.findall("item")]
    except Exception as exc:
        print(f"[sync] Error fetching shelf '{rss_shelf}': {exc}")
        return []


# ── Merge ──────────────────────────────────────────────────────────────────────

def _book_key(book: dict) -> tuple[str, str]:
    return (book.get("title", "").lower().strip(), book.get("author", "").lower().strip())


def _merge_book(existing: dict, rss: dict) -> dict:
    """
    Merge RSS data into an existing book record.
    RSS wins for most fields, but we keep the existing value when RSS returns empty —
    this preserves date_read / my_review values that Goodreads omits from older RSS entries.
    """
    merged = {**existing, **rss}
    for field in ("date_read", "date_added", "my_review", "pages"):
        if not rss.get(field) and existing.get(field):
            merged[field] = existing[field]
    return merged


def _merge(data: dict, rss_books_by_shelf: dict[str, list[dict]]) -> tuple[int, int]:
    """
    Merge RSS books into existing data in-place.
    Returns (added_count, updated_count).
    """
    # Build lookup indexes over ALL existing books
    by_gid: dict[str, tuple[str, int]] = {}   # goodreads_id → (shelf_key, list_index)
    by_key: dict[tuple, tuple[str, int]] = {}  # (title, author) → (shelf_key, list_index)

    for shelf_key in ("read", "currently_reading", "to_read"):
        for i, book in enumerate(data["books"].get(shelf_key, [])):
            gid = book.get("goodreads_id")
            if gid:
                by_gid[gid] = (shelf_key, i)
            by_key[_book_key(book)] = (shelf_key, i)

    added = updated = 0

    for shelf_key, rss_books in rss_books_by_shelf.items():
        for rss_book in rss_books:
            gid = rss_book.get("goodreads_id")
            key = _book_key(rss_book)

            # Find existing match
            match = by_gid.get(gid) if gid else None
            if match is None:
                match = by_key.get(key)

            if match is not None:
                old_shelf, idx = match
                existing = data["books"][old_shelf][idx]
                merged = _merge_book(existing, rss_book)
                if old_shelf == shelf_key:
                    # Update in place (preserves position)
                    data["books"][old_shelf][idx] = merged
                else:
                    # Book moved shelves — remove from old, prepend to new
                    data["books"][old_shelf].pop(idx)
                    data["books"][shelf_key].insert(0, merged)
                # Indexes store list positions, so any mutation can shift later matches.
                _rebuild_indexes(data, by_gid, by_key)
                updated += 1
            else:
                data["books"][shelf_key].insert(0, rss_book)
                _rebuild_indexes(data, by_gid, by_key)
                added += 1

    return added, updated


def _rebuild_indexes(data: dict, by_gid: dict, by_key: dict):
    by_gid.clear()
    by_key.clear()
    for shelf_key in ("read", "currently_reading", "to_read"):
        for i, book in enumerate(data["books"].get(shelf_key, [])):
            gid = book.get("goodreads_id")
            if gid:
                by_gid[gid] = (shelf_key, i)
            by_key[_book_key(book)] = (shelf_key, i)


def _dedupe_shelf(books: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_gid: dict[str, int] = {}
    seen_key: dict[tuple[str, str], int] = {}

    for book in books:
        gid = book.get("goodreads_id")
        key = _book_key(book)

        match_idx = seen_gid.get(gid) if gid else None
        if match_idx is None:
            match_idx = seen_key.get(key)

        if match_idx is None:
            deduped.append(book)
            idx = len(deduped) - 1
            if gid:
                seen_gid[gid] = idx
            seen_key[key] = idx
            continue

        merged = _merge_book(deduped[match_idx], book)
        deduped[match_idx] = merged
        merged_gid = merged.get("goodreads_id")
        if merged_gid:
            seen_gid[merged_gid] = match_idx
        seen_key[_book_key(merged)] = match_idx

    return deduped


def _sort_shelf(books: list[dict], *date_fields: str) -> list[dict]:
    def sort_key(book: dict) -> tuple[str, ...]:
        return tuple((book.get(field) or "").strip() for field in date_fields)

    with_date = [book for book in books if any((book.get(field) or "").strip() for field in date_fields)]
    without_date = [book for book in books if not any((book.get(field) or "").strip() for field in date_fields)]
    with_date.sort(key=sort_key, reverse=True)
    return with_date + without_date


# ── Stats ──────────────────────────────────────────────────────────────────────

def _compute_stats(data: dict) -> dict:
    read = data["books"]["read"]
    current_year = datetime.now().year
    rated = [b["my_rating"] for b in read if b.get("my_rating", 0) > 0]
    avg = round(sum(rated) / len(rated), 2) if rated else 0.0
    this_year = sum(1 for b in read if (b.get("date_read") or "").startswith(str(current_year)))
    top_authors = [
        {"author": a, "count": c}
        for a, c in Counter(b["author"] for b in read if b.get("author")).most_common(10)
    ]
    return {
        "total_read":              len(read),
        "books_this_year":         this_year,
        "avg_my_rating":           avg,
        "top_authors":             top_authors,
        "total_to_read":           len(data["books"]["to_read"]),
        "currently_reading_count": len(data["books"]["currently_reading"]),
    }


# ── Public entry point ─────────────────────────────────────────────────────────

async def sync_from_rss(user_id: str, data_file: Path) -> dict:
    """Fetch all shelves from RSS, merge into data_file, return a summary."""
    # Load existing data
    if data_file.exists():
        with data_file.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"books": {"read": [], "currently_reading": [], "to_read": []}, "stats": {}}

    # Ensure all shelf keys exist
    for k in ("read", "currently_reading", "to_read"):
        data["books"].setdefault(k, [])

    # Fetch RSS for all three shelves in parallel
    async with httpx.AsyncClient(headers={"User-Agent": "bookshelf-sync/0.5"}) as client:
        import asyncio
        results = await asyncio.gather(*[
            _fetch_shelf(client, user_id, rss_shelf, json_shelf)
            for rss_shelf, json_shelf in SHELF_MAP.items()
        ])

    rss_by_shelf = {
        json_shelf: books
        for (_, json_shelf), books in zip(SHELF_MAP.items(), results)
    }

    fetched = {k: len(v) for k, v in rss_by_shelf.items()}

    # Merge
    added, updated = _merge(data, rss_by_shelf)

    for shelf_key in ("read", "currently_reading", "to_read"):
        data["books"][shelf_key] = _dedupe_shelf(data["books"][shelf_key])

    data["books"]["read"] = _sort_shelf(data["books"]["read"], "date_read", "date_added")
    data["books"]["currently_reading"] = _sort_shelf(
        data["books"]["currently_reading"], "date_added", "date_read"
    )
    data["books"]["to_read"] = _sort_shelf(data["books"]["to_read"], "date_added")

    # Recompute stats
    data["stats"] = _compute_stats(data)
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write back atomically (write to .tmp then rename)
    tmp = data_file.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(data_file)

    return {
        "status":  "ok",
        "fetched": fetched,
        "added":   added,
        "updated": updated,
        "generated_at": data["generated_at"],
    }
