"""Google Books API lookup for book metadata."""

from __future__ import annotations

from typing import Any

import httpx

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
REQUEST_TIMEOUT = 10


def _normalize_volume(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields we care about from a Google Books volume."""
    info = item.get("volumeInfo", {})
    identifiers = {
        i["type"]: i["identifier"]
        for i in info.get("industryIdentifiers", [])
    }
    image_links = info.get("imageLinks", {})
    cover = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
    # Google returns http URLs — upgrade to https
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]

    return {
        "google_books_id": item.get("id", ""),
        "title": info.get("title", ""),
        "author": ", ".join(info.get("authors", [])),
        "isbn13": identifiers.get("ISBN_13", ""),
        "pages": info.get("pageCount"),
        "avg_rating": info.get("averageRating"),
        "cover_url": cover,
        "description": info.get("description", ""),
        "published_date": info.get("publishedDate", ""),
        "categories": info.get("categories", []),
    }


async def search_books(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search Google Books and return normalized results."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            GOOGLE_BOOKS_API,
            params={"q": query, "maxResults": max_results},
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    return [_normalize_volume(item) for item in items]
