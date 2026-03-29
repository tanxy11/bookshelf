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
import hashlib
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookshelf_data import (
    compute_books_hash,
    default_books_payload,
    default_llm_cache,
    env_truthy,
    load_json,
    load_env_file,
    merge_defaults,
    normalize_book_key,
    save_json,
    successful_recommendations,
    successful_taste_profile,
    utc_now_iso,
)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-20250514")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
REQUEST_TIMEOUT_SECONDS = 120
ROOT_DIR = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT_DIR / "scripts" / "prompts"
TASTE_PROFILE_PROMPT_FILE = "taste_profile_prompt.txt"
RECOMMENDATIONS_PROMPT_FILE = "recommendations_prompt.txt"

load_env_file(ROOT_DIR / ".env")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", OPENAI_MODEL)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_MODEL)
LLM_DRY_RUN = env_truthy("LLM_DRY_RUN", default=False)
RECOMMENDATION_PROVIDER_KEYS = ("opus", "gpt45", "gemini")
PROVIDER_ALIASES = {
    "claude": "opus",
    "anthropic": "opus",
    "opus": "opus",
    "chatgpt": "gpt45",
    "openai": "gpt45",
    "gpt": "gpt45",
    "gpt45": "gpt45",
    "gemini": "gemini",
}


def build_mock_taste_profile() -> dict[str, Any]:
    return {
        "summary": "[DRY RUN] Mock taste profile",
        "traits": [
            {
                "label": "Mock Trait",
                "explanation": "This is placeholder data.",
            }
        ],
        "blind_spots": "[DRY RUN] Mock blind spots",
    }


def build_mock_recommendations(model_name: str, prefix: str) -> dict[str, Any]:
    books = []
    for index in range(1, 6):
        books.append(
            {
                "title": f"[DRY RUN] {prefix} Pick {index}",
                "author": f"[DRY RUN] Placeholder Author {index}",
                "reason": f"[DRY RUN] Placeholder reasoning for {prefix.lower()} pick {index}.",
                "confidence": "medium",
                "from_to_read": False,
            }
        )

    return {
        "model": model_name,
        "books": books,
        "reasoning": f"[DRY RUN] Placeholder recommendation strategy from {prefix}.",
    }


def build_library_snapshot(books_payload: dict[str, Any]) -> dict[str, Any]:
    books = books_payload.get("books", {})

    def _read_entry(book: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "title": book.get("title"),
            "author": book.get("author"),
            "my_rating": book.get("my_rating"),
            "my_review": book.get("my_review"),
            "shelves": book.get("shelves", []),
            "date_read": book.get("date_read"),
        }
        notes = book.get("notes")
        if notes:
            entry["notes"] = notes
        return entry

    return {
        "stats": books_payload.get("stats", {}),
        "read": [_read_entry(book) for book in books.get("read", [])],
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


@lru_cache
def load_prompt_template(template_name: str) -> Template:
    template_path = PROMPTS_DIR / template_name
    return Template(template_path.read_text(encoding="utf-8").strip())


def render_prompt_template(template_name: str, **context: str) -> str:
    return load_prompt_template(template_name).substitute(**context)


def compute_prompt_hash() -> str:
    digest = hashlib.sha256()
    for template_name in (TASTE_PROFILE_PROMPT_FILE, RECOMMENDATIONS_PROMPT_FILE):
        digest.update(template_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(load_prompt_template(template_name).template.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_taste_profile_prompt(snapshot: dict[str, Any]) -> str:
    return render_prompt_template(
        TASTE_PROFILE_PROMPT_FILE,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, indent=2),
    )


def build_recommendations_prompt(snapshot: dict[str, Any]) -> str:
    return render_prompt_template(
        RECOMMENDATIONS_PROMPT_FILE,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, indent=2),
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


async def call_gemini_json(
    client: httpx.AsyncClient, api_key: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    response = await client.post(
        f"{GEMINI_API_BASE_URL}/{GEMINI_MODEL}:generateContent",
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.6,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        },
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {json.dumps(payload)}")

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = "\n".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")
    )
    if not text.strip():
        raise ValueError(f"Gemini returned no text payload: {json.dumps(payload)}")
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


async def generate_gemini_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
) -> dict[str, Any]:
    raw = await with_retry(
        lambda: call_gemini_json(client, api_key, build_recommendations_prompt(snapshot), 2600),
        "Gemini recommendations",
    )
    normalized = normalize_recommendations(raw, existing_books)
    normalized["model"] = GEMINI_MODEL
    return normalized


def normalize_provider_selection(raw_values: list[str] | None) -> set[str] | None:
    if not raw_values:
        return None

    selected: set[str] = set()
    for raw_value in raw_values:
        for part in raw_value.split(","):
            provider = part.strip().lower()
            if not provider:
                continue
            mapped = PROVIDER_ALIASES.get(provider)
            if mapped is None:
                valid = ", ".join(sorted(PROVIDER_ALIASES))
                raise ValueError(f"Unknown provider '{provider}'. Valid values: {valid}")
            selected.add(mapped)

    return selected or None


def skip_generation(
    cache_payload: dict[str, Any],
    books_hash: str,
    force: bool,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool | None = None,
) -> bool:
    if refresh_taste_profile is None:
        refresh_taste_profile = selected_providers is None

    if force or cache_payload.get("books_hash") != books_hash:
        return False

    recommendations = cache_payload.get("recommendations") or {}
    cache_dry_run = bool(cache_payload.get("dry_run"))
    prompt_hash = cache_payload.get("prompt_hash")
    if cache_dry_run != LLM_DRY_RUN or prompt_hash != compute_prompt_hash():
        return False

    if selected_providers is None:
        if cache_payload.get("partial_refresh"):
            return False
        return (
            (recommendations.get("opus") or {}).get("model") == ANTHROPIC_MODEL
            and (recommendations.get("gpt45") or {}).get("model") == OPENAI_MODEL
            and (recommendations.get("gemini") or {}).get("model") == GEMINI_MODEL
        )

    if refresh_taste_profile:
        taste_profile = cache_payload.get("taste_profile") or {}
        if taste_profile.get("error") or not taste_profile.get("summary"):
            return False

    runtime_models = {
        "opus": ANTHROPIC_MODEL,
        "gpt45": OPENAI_MODEL,
        "gemini": GEMINI_MODEL,
    }
    for provider in selected_providers:
        entry = recommendations.get(provider) or {}
        if entry.get("model") != runtime_models[provider]:
            return False
        if entry.get("error") or not entry.get("books"):
            return False

    return True


async def generate_cache_payload(
    books_payload: dict[str, Any],
    cache_payload: dict[str, Any],
    force: bool = False,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool | None = None,
) -> tuple[dict[str, Any], bool]:
    if refresh_taste_profile is None:
        refresh_taste_profile = selected_providers is None

    books_hash = compute_books_hash(books_payload)
    if skip_generation(
        cache_payload,
        books_hash,
        force,
        selected_providers=selected_providers,
        refresh_taste_profile=refresh_taste_profile,
    ):
        return cache_payload, True

    snapshot = build_library_snapshot(books_payload)
    all_books = {
        normalize_book_key(book.get("title", ""), book.get("author", ""))
        for shelf in ("read", "currently_reading")
        for book in books_payload.get("books", {}).get(shelf, [])
    }

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    target_providers = (
        tuple(provider for provider in RECOMMENDATION_PROVIDER_KEYS if provider in selected_providers)
        if selected_providers is not None
        else RECOMMENDATION_PROVIDER_KEYS
    )

    result = (
        default_llm_cache()
        if selected_providers is None and refresh_taste_profile
        else merge_defaults(default_llm_cache(), cache_payload)
    )
    result["books_hash"] = books_hash
    result["generated_at"] = utc_now_iso()
    result["dry_run"] = LLM_DRY_RUN
    result["prompt_hash"] = compute_prompt_hash()
    result["partial_refresh"] = not (selected_providers is None and refresh_taste_profile)
    runtime_models = {
        "opus": ANTHROPIC_MODEL,
        "gpt45": OPENAI_MODEL,
        "gemini": GEMINI_MODEL,
    }
    for provider in target_providers:
        result["recommendations"][provider]["model"] = runtime_models[provider]

    if LLM_DRY_RUN:
        if refresh_taste_profile:
            result["taste_profile"] = build_mock_taste_profile()
        if "opus" in target_providers:
            result["recommendations"]["opus"] = build_mock_recommendations(
                ANTHROPIC_MODEL, "Anthropic"
            )
        if "gpt45" in target_providers:
            result["recommendations"]["gpt45"] = build_mock_recommendations(
                OPENAI_MODEL, "OpenAI"
            )
        if "gemini" in target_providers:
            result["recommendations"]["gemini"] = build_mock_recommendations(
                GEMINI_MODEL, "Gemini"
            )
        return result, False

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if refresh_taste_profile:
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

        recommendation_tasks: list[tuple[str, asyncio.Future | Any | None, str]] = []
        provider_factories = {
            "opus": (
                anthropic_key,
                lambda: generate_anthropic_recommendations(client, snapshot, anthropic_key, all_books),
                "ANTHROPIC_API_KEY is not set.",
            ),
            "gpt45": (
                openai_key,
                lambda: generate_openai_recommendations(client, snapshot, openai_key, all_books),
                "OPENAI_API_KEY is not set.",
            ),
            "gemini": (
                gemini_key,
                lambda: generate_gemini_recommendations(client, snapshot, gemini_key, all_books),
                "GEMINI_API_KEY or GOOGLE_API_KEY is not set.",
            ),
        }

        for provider in target_providers:
            api_key, factory, missing_message = provider_factories[provider]
            if api_key:
                recommendation_tasks.append((provider, factory(), runtime_models[provider]))
            else:
                result["recommendations"][provider] = {
                    "model": runtime_models[provider],
                    "error": missing_message,
                }
                recommendation_tasks.append((provider, None, runtime_models[provider]))

        active_tasks = [task for _, task, _ in recommendation_tasks if task is not None]
        responses = await asyncio.gather(*active_tasks, return_exceptions=True)

        response_index = 0
        for provider_key, task, model_name in recommendation_tasks:
            if task is None:
                continue
            response = responses[response_index]
            response_index += 1
            if isinstance(response, Exception):
                result["recommendations"][provider_key] = {
                    "model": model_name,
                    "error": str(response),
                }
            else:
                result["recommendations"][provider_key] = response

    return result, False


def _save_llm_cache_to_db(conn: Any, payload: dict[str, Any]) -> None:
    """Write LLM cache payload to SQLite llm_cache table as separate keys."""
    from db import set_llm_cache_value

    set_llm_cache_value(conn, "metadata", {
        "books_hash": payload.get("books_hash", ""),
        "generated_at": payload.get("generated_at"),
        "dry_run": payload.get("dry_run", False),
        "prompt_hash": payload.get("prompt_hash", ""),
        "partial_refresh": payload.get("partial_refresh", False),
    })
    set_llm_cache_value(conn, "taste_profile", payload.get("taste_profile", {}))
    set_llm_cache_value(conn, "recommendations", payload.get("recommendations", {}))


def _print_result(label: str, payload: dict[str, Any]) -> None:
    taste_ok = successful_taste_profile(payload) is not None
    recommendations_ok = successful_recommendations(payload) is not None

    print(f"Wrote {label}")
    print(f"  books_hash: {payload.get('books_hash')}")
    print(f"  dry_run: {'true' if payload.get('dry_run') else 'false'}")
    print(f"  taste_profile: {'ok' if taste_ok else 'error'}")
    recommendation_status = ", ".join(
        f"{provider_key}={'ok' if (payload['recommendations'].get(provider_key) or {}).get('books') else 'error'}"
        for provider_key in ("opus", "gpt45", "gemini")
    )
    print(
        f"  recommendations: {recommendation_status}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate cached LLM content.")
    parser.add_argument("--books", default="data/books.json", help="Path to books.json")
    parser.add_argument("--cache", default="data/llm_cache.json", help="Path to llm_cache.json")
    parser.add_argument("--db", default=None, help="Path to SQLite database (overrides --books/--cache)")
    parser.add_argument("--force", action="store_true", help="Always regenerate, ignoring books_hash")
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Recommendation provider(s) to refresh: claude, chatgpt, gemini. Repeat or comma-separate.",
    )
    parser.add_argument(
        "--with-taste-profile",
        action="store_true",
        help="When using --provider, also refresh the Anthropic taste profile.",
    )
    args = parser.parse_args()
    try:
        args.providers = normalize_provider_selection(args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()

    db_path = args.db or os.getenv("DB_PATH", "").strip()
    if db_path and Path(db_path).exists():
        return _main_sqlite(
            Path(db_path),
            force=args.force,
            selected_providers=args.providers,
            refresh_taste_profile=args.with_taste_profile,
        )
    return _main_json(
        Path(args.books),
        Path(args.cache),
        force=args.force,
        selected_providers=args.providers,
        refresh_taste_profile=args.with_taste_profile,
    )


def _main_sqlite(
    db_path: Path,
    force: bool,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool = False,
) -> int:
    from bookshelf_data import BookshelfDB

    store = BookshelfDB(db_path)
    books_payload = store.books()
    cache_payload = store.llm_cache()

    generated_payload, skipped = asyncio.run(
        generate_cache_payload(
            books_payload,
            cache_payload,
            force=force,
            selected_providers=selected_providers,
            refresh_taste_profile=refresh_taste_profile,
        )
    )

    if skipped:
        print(f"LLM cache is up to date for hash {generated_payload.get('books_hash')}. Skipping.")
        return 0

    _save_llm_cache_to_db(store.conn(), generated_payload)
    _print_result(f"llm_cache → {db_path}", generated_payload)

    taste_ok = successful_taste_profile(generated_payload) is not None
    recommendations_ok = successful_recommendations(generated_payload) is not None
    return 0 if taste_ok or recommendations_ok else 1


def _main_json(
    books_path: Path,
    cache_path: Path,
    force: bool,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool = False,
) -> int:
    if not books_path.exists():
        print(f"Error: books data not found: {books_path}", file=sys.stderr)
        return 1

    books_payload = load_json(books_path, default_books_payload)
    cache_payload = load_json(cache_path, default_llm_cache)

    generated_payload, skipped = asyncio.run(
        generate_cache_payload(
            books_payload,
            cache_payload,
            force=force,
            selected_providers=selected_providers,
            refresh_taste_profile=refresh_taste_profile,
        )
    )

    if skipped:
        print(f"LLM cache is up to date for hash {generated_payload.get('books_hash')}. Skipping.")
        return 0

    save_json(cache_path, generated_payload)
    _print_result(str(cache_path), generated_payload)

    taste_ok = successful_taste_profile(generated_payload) is not None
    recommendations_ok = successful_recommendations(generated_payload) is not None
    return 0 if taste_ok or recommendations_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
