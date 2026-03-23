#!/usr/bin/env python3
"""
Generate cached LLM content for tanxy.net/book.

Usage:
    python scripts/generate_llm.py --books data/books.json --cache data/llm_cache.json
    python scripts/generate_llm.py --force
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookshelf_data import (
    compute_books_hash,
    default_books_payload,
    default_llm_cache,
    load_json,
    load_env_file,
    normalize_book_key,
    save_json,
    successful_recommendations,
    successful_taste_profile,
    utc_now_iso,
)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-20250514")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
REQUEST_TIMEOUT_SECONDS = 120
ROOT_DIR = Path(__file__).resolve().parents[1]

load_env_file(ROOT_DIR / ".env")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", OPENAI_MODEL)


def build_library_snapshot(books_payload: dict[str, Any]) -> dict[str, Any]:
    books = books_payload.get("books", {})
    return {
        "stats": books_payload.get("stats", {}),
        "read": [
            {
                "title": book.get("title"),
                "author": book.get("author"),
                "my_rating": book.get("my_rating"),
                "my_review": book.get("my_review"),
                "shelves": book.get("shelves", []),
                "date_read": book.get("date_read"),
            }
            for book in books.get("read", [])
        ],
        "currently_reading": [
            {
                "title": book.get("title"),
                "author": book.get("author"),
            }
            for book in books.get("currently_reading", [])
        ],
        "to_read": [
            {
                "title": book.get("title"),
                "author": book.get("author"),
            }
            for book in books.get("to_read", [])
        ],
    }


def build_taste_profile_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "You are analyzing a real person's reading history.\n"
        "Return strict JSON only with this shape:\n"
        '{\n'
        '  "summary": "2-3 sentences",\n'
        '  "traits": [\n'
        '    {"label": "short trait label", "explanation": "one sentence explanation"}\n'
        "  ],\n"
        '  "blind_spots": "1-2 sentences"\n'
        "}\n\n"
        "Rules:\n"
        "- Base every claim on patterns actually visible in the data.\n"
        "- Use ratings, shelves, and review text when available.\n"
        "- Avoid generic genre summaries and empty flattery.\n"
        "- Traits should describe reading personality, not genres.\n"
        "- If the data is thin or ambiguous, say that honestly.\n"
        "- Keep the tone warm, insightful, and slightly playful.\n\n"
        f"Reading data:\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
    )


def build_recommendations_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "You are recommending books to a specific reader based on their real reading history.\n"
        "Return strict JSON only with this shape:\n"
        '{\n'
        '  "books": [\n'
        '    {\n'
        '      "title": "Book title",\n'
        '      "author": "Author name",\n'
        '      "reason": "2-3 sentences tied to concrete books, reviews, or patterns in the shelf",\n'
        '      "confidence": "high",\n'
        '      "from_to_read": false\n'
        "    }\n"
        "  ],\n"
        '  "reasoning": "3-4 sentences about the overall strategy"\n'
        "}\n\n"
        "Rules:\n"
        "- Recommend exactly 5 books if possible.\n"
        "- Do not recommend any book already present in read or currently_reading.\n"
        "- You MAY recommend books from the to_read shelf — if you do, set from_to_read to true and explain why that book is particularly well-suited given the reading history.\n"
        "- Use the reader's reviews (my_review field) as the primary evidence for their preferences — reviews reveal what they actually valued or disliked, not just what they finished.\n"
        "- Treat high ratings without a review as weaker signal than a detailed review at any rating.\n"
        "- Explain each recommendation by referencing specific books, reviews, or patterns from the library.\n"
        '- Do not use generic phrases like "if you liked this genre".\n'
        '- Confidence must be one of: "high", "medium", "low".\n'
        "- If the data is ambiguous, lower confidence rather than inventing certainty.\n"
        "- Keep output concise but specific.\n\n"
        f"Reading data:\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
    )


def strip_code_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(raw: str) -> dict[str, Any]:
    text = strip_code_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def normalize_taste_profile(payload: dict[str, Any]) -> dict[str, Any]:
    summary = " ".join(str(payload.get("summary") or "").split())
    blind_spots = " ".join(str(payload.get("blind_spots") or "").split())
    traits: list[dict[str, str]] = []
    for item in payload.get("traits") or []:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or item.get("name") or "").split())
        explanation = " ".join(
            str(item.get("explanation") or item.get("description") or "").split()
        )
        if label and explanation:
            traits.append({"label": label, "explanation": explanation})

    if not summary or not blind_spots or len(traits) < 1:
        raise ValueError("Taste profile response was missing required fields.")

    return {
        "summary": summary,
        "traits": traits[:5],
        "blind_spots": blind_spots,
    }


def normalize_recommendations(
    payload: dict[str, Any], existing_books: set[tuple[str, str]]
) -> dict[str, Any]:
    cleaned_books: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for item in payload.get("books") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        author = " ".join(str(item.get("author") or "").split())
        reason = " ".join(str(item.get("reason") or "").split())
        confidence = str(item.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"

        key = normalize_book_key(title, author)
        if not title or not author or not reason or key in seen or key in existing_books:
            continue

        from_to_read = bool(item.get("from_to_read"))
        seen.add(key)
        cleaned_books.append(
            {
                "title": title,
                "author": author,
                "reason": reason,
                "confidence": confidence,
                "from_to_read": from_to_read,
            }
        )

    reasoning = " ".join(str(payload.get("reasoning") or "").split())
    if not cleaned_books or not reasoning:
        raise ValueError("Recommendations response was missing usable books or reasoning.")

    return {"books": cleaned_books[:5], "reasoning": reasoning}


async def call_anthropic_json(
    client: httpx.AsyncClient, api_key: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    response = await client.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "temperature": 0.5,
            "system": "Return valid JSON only. Do not wrap it in markdown.",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    payload = response.json()
    text_blocks = [
        block.get("text", "")
        for block in payload.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return extract_json_object("\n".join(text_blocks))


async def call_openai_json(
    client: httpx.AsyncClient, api_key: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    response = await client.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "temperature": 0.6,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": "Return valid JSON only. Do not use markdown."},
                {"role": "user", "content": prompt},
            ],
        },
    )
    response.raise_for_status()
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    if isinstance(content, list):
        text = "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = content or ""
    return extract_json_object(text)


async def with_retry(coro_factory, label: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - surface clean provider errors to cache
            last_error = exc
            if attempt < 2:
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                    if status_code == 429:
                        retry_after = exc.response.headers.get("retry-after")
                        delay = float(retry_after) if retry_after else 60.0
                        await asyncio.sleep(delay)
                    elif status_code in {500, 502, 503, 504}:
                        await asyncio.sleep(2 * (attempt + 1))
                continue
    raise RuntimeError(f"{label} failed: {last_error}") from last_error


async def generate_taste_profile(
    client: httpx.AsyncClient, snapshot: dict[str, Any], api_key: str
) -> dict[str, Any]:
    raw = await with_retry(
        lambda: call_anthropic_json(client, api_key, build_taste_profile_prompt(snapshot), 1800),
        "Taste profile generation",
    )
    return normalize_taste_profile(raw)


async def generate_anthropic_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
) -> dict[str, Any]:
    raw = await with_retry(
        lambda: call_anthropic_json(client, api_key, build_recommendations_prompt(snapshot), 2600),
        "Anthropic recommendations",
    )
    normalized = normalize_recommendations(raw, existing_books)
    normalized["model"] = ANTHROPIC_MODEL
    return normalized


async def generate_openai_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
) -> dict[str, Any]:
    raw = await with_retry(
        lambda: call_openai_json(client, api_key, build_recommendations_prompt(snapshot), 2600),
        "OpenAI recommendations",
    )
    normalized = normalize_recommendations(raw, existing_books)
    normalized["model"] = OPENAI_MODEL
    return normalized


def skip_generation(cache_payload: dict[str, Any], books_hash: str, force: bool) -> bool:
    return not force and cache_payload.get("books_hash") == books_hash


async def generate_cache_payload(
    books_payload: dict[str, Any],
    cache_payload: dict[str, Any],
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    read_books = books_payload.get("books", {}).get("read", [])
    books_hash = compute_books_hash(read_books)
    if skip_generation(cache_payload, books_hash, force):
        return cache_payload, True

    snapshot = build_library_snapshot(books_payload)
    all_books = {
        normalize_book_key(book.get("title", ""), book.get("author", ""))
        for shelf in ("read", "currently_reading")
        for book in books_payload.get("books", {}).get(shelf, [])
    }

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    result = default_llm_cache()
    result["books_hash"] = books_hash
    result["generated_at"] = utc_now_iso()
    result["recommendations"]["opus"]["model"] = ANTHROPIC_MODEL
    result["recommendations"]["gpt45"]["model"] = OPENAI_MODEL

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if anthropic_key:
            try:
                result["taste_profile"] = await generate_taste_profile(client, snapshot, anthropic_key)
            except Exception as exc:  # noqa: BLE001
                result["taste_profile"] = {"error": str(exc), "model": ANTHROPIC_MODEL}
        else:
            result["taste_profile"] = {
                "error": "ANTHROPIC_API_KEY is not set.",
                "model": ANTHROPIC_MODEL,
            }

        recommendation_tasks = [
            generate_anthropic_recommendations(client, snapshot, anthropic_key, all_books)
            if anthropic_key
            else None,
            generate_openai_recommendations(client, snapshot, openai_key, all_books)
            if openai_key
            else None,
        ]

        if recommendation_tasks[0] is None:
            result["recommendations"]["opus"] = {
                "model": ANTHROPIC_MODEL,
                "error": "ANTHROPIC_API_KEY is not set.",
            }
        if recommendation_tasks[1] is None:
            result["recommendations"]["gpt45"] = {
                "model": OPENAI_MODEL,
                "error": "OPENAI_API_KEY is not set.",
            }

        active_tasks = [task for task in recommendation_tasks if task is not None]
        responses = await asyncio.gather(*active_tasks, return_exceptions=True)

        response_index = 0
        for provider_key, task in (("opus", recommendation_tasks[0]), ("gpt45", recommendation_tasks[1])):
            if task is None:
                continue
            response = responses[response_index]
            response_index += 1
            if isinstance(response, Exception):
                result["recommendations"][provider_key] = {
                    "model": ANTHROPIC_MODEL if provider_key == "opus" else OPENAI_MODEL,
                    "error": str(response),
                }
            else:
                result["recommendations"][provider_key] = response

    return result, False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate cached LLM content.")
    parser.add_argument("--books", default="data/books.json", help="Path to books.json")
    parser.add_argument("--cache", default="data/llm_cache.json", help="Path to llm_cache.json")
    parser.add_argument("--force", action="store_true", help="Always regenerate, ignoring books_hash")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    books_path = Path(args.books)
    cache_path = Path(args.cache)

    if not books_path.exists():
        print(f"Error: books data not found: {books_path}", file=sys.stderr)
        return 1

    books_payload = load_json(books_path, default_books_payload)
    cache_payload = load_json(cache_path, default_llm_cache)

    generated_payload, skipped = asyncio.run(
        generate_cache_payload(books_payload, cache_payload, force=args.force)
    )

    if skipped:
        print(f"LLM cache is up to date for hash {generated_payload.get('books_hash')}. Skipping.")
        return 0

    save_json(cache_path, generated_payload)

    taste_ok = successful_taste_profile(generated_payload) is not None
    recommendations_ok = successful_recommendations(generated_payload) is not None

    print(f"Wrote {cache_path}")
    print(f"  books_hash: {generated_payload.get('books_hash')}")
    print(f"  taste_profile: {'ok' if taste_ok else 'error'}")
    print(
        "  recommendations: "
        f"opus={'ok' if generated_payload['recommendations']['opus'].get('books') else 'error'}, "
        f"gpt45={'ok' if generated_payload['recommendations']['gpt45'].get('books') else 'error'}"
    )

    if taste_ok or recommendations_ok:
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
