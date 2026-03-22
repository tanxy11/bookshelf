#!/usr/bin/env python3
"""
Parse a Goodreads CSV export into books.json for tanxy.net.

Usage:
    python scripts/parse_goodreads.py \
        --input data/goodreads_library_export.csv \
        --output data/books.json
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def clean_title(title: str) -> str:
    """Strip trailing series info in parentheses, e.g. 'Dune (Dune #1)' -> 'Dune'."""
    return re.sub(r"\s*\([^)]*#\d+[^)]*\)\s*$", "", title).strip()


def parse_isbn(raw: str) -> str:
    """Strip surrounding = and quotes that Goodreads wraps around ISBNs."""
    return re.sub(r'[="\'=]', "", raw).strip()


def parse_date(raw: str) -> str:
    """Normalise various date formats to YYYY-MM-DD or empty string."""
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # return as-is if we can't parse


def parse_shelves(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def parse_book(row: dict) -> dict:
    isbn13 = parse_isbn(row.get("ISBN13", "") or row.get("ISBN", ""))
    my_rating_raw = row.get("My Rating", "0").strip()
    avg_rating_raw = row.get("Average Rating", "0").strip()
    pages_raw = row.get("Number of Pages", "").strip()

    try:
        my_rating = int(my_rating_raw)
    except ValueError:
        my_rating = 0

    try:
        avg_rating = round(float(avg_rating_raw), 2)
    except ValueError:
        avg_rating = None

    try:
        pages = int(pages_raw)
    except ValueError:
        pages = None

    return {
        "title": clean_title(row.get("Title", "").strip()),
        "author": row.get("Author", "").strip(),
        "isbn13": isbn13,
        "my_rating": my_rating,
        "avg_rating": avg_rating,
        "pages": pages,
        "date_read": parse_date(row.get("Date Read", "")),
        "date_added": parse_date(row.get("Date Added", "")),
        "shelves": parse_shelves(row.get("Bookshelves", "")),
        "exclusive_shelf": row.get("Exclusive Shelf", "").strip(),
        "my_review": row.get("My Review", "").strip() or None,
    }




def compute_stats(read_books: list[dict]) -> dict:
    current_year = datetime.now().year

    rated = [b["my_rating"] for b in read_books if b["my_rating"] > 0]
    avg_my_rating = round(sum(rated) / len(rated), 2) if rated else 0.0

    books_this_year = sum(
        1
        for b in read_books
        if b.get("date_read", "").startswith(str(current_year))
    )

    author_counts = Counter(b["author"] for b in read_books if b["author"])
    top_authors = [
        {"author": a, "count": c}
        for a, c in author_counts.most_common(10)
    ]

    return {
        "total_read": len(read_books),
        "books_this_year": books_this_year,
        "avg_my_rating": avg_my_rating,
        "top_authors": top_authors,
    }


def main():
    parser = argparse.ArgumentParser(description="Parse Goodreads CSV export.")
    parser.add_argument(
        "--input",
        default="data/goodreads_library_export.csv",
        help="Path to Goodreads CSV export",
    )
    parser.add_argument(
        "--output",
        default="data/books.json",
        help="Output path for books.json",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    collections: dict[str, list] = {
        "read": [],
        "currently_reading": [],
        "to_read": [],
    }

    with input_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            book = parse_book(row)
            shelf = book["exclusive_shelf"]
            if shelf == "read":
                collections["read"].append(book)
            elif shelf == "currently-reading":
                collections["currently_reading"].append(book)
            elif shelf == "to-read":
                collections["to_read"].append(book)
            # skip unrecognised shelves

    # Sort read books by date_read descending (most recent first); no-date books go last
    with_date = [b for b in collections["read"] if b.get("date_read")]
    without_date = [b for b in collections["read"] if not b.get("date_read")]
    with_date.sort(key=lambda b: b["date_read"], reverse=True)
    collections["read"] = with_date + without_date

    stats = compute_stats(collections["read"])
    stats["total_to_read"] = len(collections["to_read"])
    stats["currently_reading_count"] = len(collections["currently_reading"])

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "books": collections,
        "stats": stats,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Done. Wrote {output_path}")
    print(
        f"  read: {len(collections['read'])}, "
        f"currently-reading: {len(collections['currently_reading'])}, "
        f"to-read: {len(collections['to_read'])}"
    )


if __name__ == "__main__":
    main()
