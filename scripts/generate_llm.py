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
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookshelf_data import (
    LLM_TARGET_KEYS,
    RECOMMENDATION_TARGET_KEYS,
    compute_llm_input_hash,
    default_books_payload,
    default_llm_cache,
    default_target_generated_at,
    default_target_input_hashes,
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
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "6000"))
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "medium").strip().lower() or "medium"

load_env_file(ROOT_DIR / ".env")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", OPENAI_MODEL)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_MODEL)
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", str(GEMINI_MAX_OUTPUT_TOKENS)))
GEMINI_THINKING_LEVEL = (
    os.getenv("GEMINI_THINKING_LEVEL", GEMINI_THINKING_LEVEL).strip().lower() or "medium"
)
LLM_DRY_RUN = env_truthy("LLM_DRY_RUN", default=False)
RECOMMENDATION_PROVIDER_KEYS = RECOMMENDATION_TARGET_KEYS
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
TASTE_PROFILE_PROVIDER_ALIASES = {
    "claude": "opus",
    "anthropic": "opus",
    "opus": "opus",
    "chatgpt": "gpt45",
    "openai": "gpt45",
    "gpt": "gpt45",
    "gpt45": "gpt45",
}


def _selected_targets(
    selected_providers: set[str] | None,
    refresh_taste_profile: bool,
) -> tuple[str, ...]:
    targets: list[str] = []
    if refresh_taste_profile:
        targets.append("taste_profile")

    if selected_providers is None:
        targets.extend(RECOMMENDATION_PROVIDER_KEYS)
    else:
        targets.extend(
            provider for provider in RECOMMENDATION_PROVIDER_KEYS if provider in selected_providers
        )

    return tuple(targets)


def _is_full_refresh(
    selected_providers: set[str] | None,
    refresh_taste_profile: bool,
) -> bool:
    if not refresh_taste_profile:
        return False
    if selected_providers is None:
        return True
    return set(selected_providers) == set(RECOMMENDATION_PROVIDER_KEYS)


def _target_input_hashes(cache_payload: dict[str, Any]) -> dict[str, str]:
    legacy_hash = str(cache_payload.get("llm_input_hash") or cache_payload.get("books_hash") or "")
    raw = cache_payload.get("target_input_hashes")
    if not isinstance(raw, dict):
        raw = {}
    return {
        target: str(raw.get(target) or legacy_hash or "")
        for target in LLM_TARGET_KEYS
    }


def _target_generated_at(cache_payload: dict[str, Any]) -> dict[str, str | None]:
    legacy_generated_at = cache_payload.get("generated_at")
    raw = cache_payload.get("target_generated_at")
    if not isinstance(raw, dict):
        raw = {}
    return {
        target: raw.get(target) or legacy_generated_at
        for target in LLM_TARGET_KEYS
    }


class GeminiRecommendationBook(BaseModel):
    title: str = Field(description="Book title.")
    author: str = Field(description="Book author.")
    reason: str = Field(description="Why this recommendation fits the reader.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence level for the recommendation."
    )
    from_to_read: bool = Field(
        description="Whether the book is already on the reader's to-read shelf."
    )


class GeminiRecommendationsPayload(BaseModel):
    reasoning: str = Field(
        description="A short paragraph explaining the overall recommendation strategy."
    )
    books: list[GeminiRecommendationBook] = Field(
        description="Up to 5 recommended books for this reader."
    )


GEMINI_RECOMMENDATIONS_JSON_SCHEMA = GeminiRecommendationsPayload.model_json_schema()


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, debug_info: dict[str, Any] | None = None):
        super().__init__(message)
        self.debug_info = debug_info or {}


def _debug_excerpt(value: str, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _debug_payload_excerpt(payload: Any) -> str:
    try:
        return _debug_excerpt(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return _debug_excerpt(repr(payload))


def _http_error_excerpt(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
    except Exception:  # noqa: BLE001
        return _debug_excerpt(exc.response.text)
    return _debug_payload_excerpt(payload)


def _provider_debug_info(
    model_name: str,
    response_payload: dict[str, Any],
    raw_text: str,
    finish_reason: str | None = None,
    usage_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    debug = {
        "model": model_name,
        "captured_at": utc_now_iso(),
        "response_excerpt": _debug_payload_excerpt(response_payload),
        "raw_text_excerpt": _debug_excerpt(raw_text),
    }
    if finish_reason:
        debug["finish_reason"] = finish_reason
    if usage_metadata:
        debug["usage_metadata"] = usage_metadata
    return debug


def build_mock_taste_profile(
    model_name: str = ANTHROPIC_MODEL,
    provider: str = "opus",
) -> dict[str, Any]:
    return {
        "model": model_name,
        "provider": provider,
        "summary": "[DRY RUN] Mock taste profile",
        "current_drift": "[DRY RUN] Mock current drift",
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

RECENT_READ_LIMIT = 50
CURRENT_READING_NOTE_LIMIT = 2
HISTORICAL_ANCHOR_LIMIT = 25
HISTORICAL_SAMPLE_LIMIT = 50


def _book_identity(book: dict[str, Any]) -> tuple[str, str]:
    return normalize_book_key(book.get("title", ""), book.get("author", ""))


def _book_entry(
    book: dict[str, Any],
    *,
    include_notes: bool = False,
    notes_limit: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "title": book.get("title"),
        "author": book.get("author"),
        "my_rating": book.get("my_rating"),
        "my_review": book.get("my_review"),
        "shelves": book.get("shelves", []),
        "date_read": book.get("date_read"),
        "date_added": book.get("date_added"),
        "read_events": book.get("read_events", []),
        "note_count": int(book.get("note_count") or 0),
    }
    if include_notes:
        entry["notes"] = [
            {
                "note_type": note.get("note_type"),
                "content": note.get("content"),
                "page_or_location": note.get("page_or_location"),
                "created_at": note.get("created_at"),
            }
            for note in (book.get("notes") or [])[:notes_limit]
            if isinstance(note, dict) and str(note.get("content") or "").strip()
        ]
    if extra:
        entry.update(extra)
    return entry


def _read_completion_count(book: dict[str, Any]) -> int:
    return sum(
        1
        for event in (book.get("read_events") or [])
        if isinstance(event, dict) and event.get("finished_on")
    )


def _historical_anchor_score(book: dict[str, Any]) -> float:
    rating = int(book.get("my_rating") or 0)
    review_length = len(str(book.get("my_review") or ""))
    note_count = int(book.get("note_count") or 0)
    reread_count = max(0, _read_completion_count(book) - 1)
    return (
        rating * 20
        + min(review_length / 120, 20)
        + min(note_count * 8, 32)
        + reread_count * 14
    )


def _historical_anchor_reasons(book: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if int(book.get("my_rating") or 0) >= 5:
        reasons.append("high_rating")
    if len(str(book.get("my_review") or "")) >= 600:
        reasons.append("substantial_review")
    if int(book.get("note_count") or 0) > 0:
        reasons.append("has_notes")
    if _read_completion_count(book) > 1:
        reasons.append("reread")
    return reasons or ["representative_older_read"]


def _stable_sample_key(book: dict[str, Any]) -> str:
    title = str(book.get("title") or "").strip().lower()
    author = str(book.get("author") or "").strip().lower()
    return hashlib.sha256(f"taste-profile-sample-v1\0{title}\0{author}".encode("utf-8")).hexdigest()


def build_taste_profile_snapshot(books_payload: dict[str, Any]) -> dict[str, Any]:
    books = books_payload.get("books", {})
    read_books = [book for book in books.get("read", []) if isinstance(book, dict)]
    currently_reading = [
        book
        for book in books.get("currently_reading", [])
        if isinstance(book, dict)
        and any(str(note.get("content") or "").strip() for note in (book.get("notes") or []))
    ]

    recent_read_books = read_books[:RECENT_READ_LIMIT]
    recent_keys = {_book_identity(book) for book in recent_read_books}
    older_read_books = [book for book in read_books if _book_identity(book) not in recent_keys]

    anchors = sorted(
        older_read_books,
        key=lambda book: (-_historical_anchor_score(book), str(book.get("date_read") or "")),
    )[:HISTORICAL_ANCHOR_LIMIT]
    anchor_keys = {_book_identity(book) for book in anchors}
    sample_pool = [book for book in older_read_books if _book_identity(book) not in anchor_keys]
    historical_sample = sorted(sample_pool, key=_stable_sample_key)[:HISTORICAL_SAMPLE_LIMIT]

    return {
        "stats": books_payload.get("stats", {}),
        "selection_strategy": {
            "recent_read_books": (
                f"Most recent {RECENT_READ_LIMIT} completed books, with ratings, shelves, "
                "reviews, read dates, and note counts."
            ),
            "currently_reading_with_notes": (
                "Only in-progress books with personal notes. These are high-signal but "
                "provisional evidence of current preoccupations."
            ),
            "historical_anchors": (
                f"Up to {HISTORICAL_ANCHOR_LIMIT} older completed books selected for high "
                "rating, substantial review text, note count, or rereading."
            ),
            "historical_sample": (
                f"A deterministic sample of up to {HISTORICAL_SAMPLE_LIMIT} older completed "
                "books not already selected as anchors."
            ),
        },
        "recent_read_books": [_book_entry(book) for book in recent_read_books],
        "currently_reading_with_notes": [
            _book_entry(
                book,
                include_notes=True,
                notes_limit=CURRENT_READING_NOTE_LIMIT,
                extra={"evidence_status": "in_progress"},
            )
            for book in currently_reading
        ],
        "historical_anchors": [
            _book_entry(
                book,
                include_notes=True,
                notes_limit=1,
                extra={
                    "anchor_score": round(_historical_anchor_score(book), 2),
                    "anchor_reasons": _historical_anchor_reasons(book),
                },
            )
            for book in anchors
        ],
        "historical_sample": [_book_entry(book) for book in historical_sample],
        "excluded_counts": {
            "read_books_not_in_snapshot": max(
                0,
                len(read_books)
                - len(recent_read_books)
                - len(anchors)
                - len(historical_sample),
            ),
            "currently_reading_without_notes": max(
                0,
                len([book for book in books.get("currently_reading", []) if isinstance(book, dict)])
                - len(currently_reading),
            ),
        },
    }


def build_library_snapshot(books_payload: dict[str, Any]) -> dict[str, Any]:
    books = books_payload.get("books", {})

    return {
        "stats": books_payload.get("stats", {}),
        "read": [_book_entry(book) for book in books.get("read", [])],
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
    current_drift = " ".join(str(payload.get("current_drift") or "").split())
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
        "current_drift": current_drift,
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
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    raw_text = "\n".join(text_blocks)
    debug_info = _provider_debug_info(ANTHROPIC_MODEL, payload, raw_text)
    try:
        return extract_json_object(raw_text), debug_info
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=debug_info) from exc


async def call_openai_json(
    client: httpx.AsyncClient, api_key: str, prompt: str, max_tokens: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_payload = {
        "model": OPENAI_MODEL,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Do not use markdown."},
            {"role": "user", "content": prompt},
        ],
    }
    response = await client.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=request_payload,
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
    debug_info = _provider_debug_info(OPENAI_MODEL, payload, text)
    try:
        return extract_json_object(text), debug_info
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=debug_info) from exc


async def call_gemini_json(
    client: httpx.AsyncClient, api_key: str, prompt: str, max_tokens: int
) -> tuple[dict[str, Any], dict[str, Any]]:
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
                "thinkingConfig": {
                    "thinkingLevel": GEMINI_THINKING_LEVEL,
                },
                "responseMimeType": "application/json",
                "responseJsonSchema": GEMINI_RECOMMENDATIONS_JSON_SCHEMA,
            },
        },
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates") or []
    finish_reason = candidates[0].get("finishReason") if candidates else None
    usage_metadata = payload.get("usageMetadata")
    if not candidates:
        raise ProviderResponseError(
            f"Gemini returned no candidates: {_debug_payload_excerpt(payload)}",
            debug_info=_provider_debug_info(
                GEMINI_MODEL,
                payload,
                "",
                finish_reason=finish_reason,
                usage_metadata=usage_metadata,
            ),
        )

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = "\n".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")
    )
    debug_info = _provider_debug_info(
        GEMINI_MODEL,
        payload,
        text,
        finish_reason=finish_reason,
        usage_metadata=usage_metadata,
    )
    debug_info["thinking_level"] = GEMINI_THINKING_LEVEL
    debug_info["max_output_tokens"] = max_tokens
    if not text.strip():
        raise ProviderResponseError(
            f"Gemini returned no text payload: {_debug_payload_excerpt(payload)}",
            debug_info=debug_info,
        )
    try:
        return extract_json_object(text), debug_info
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=debug_info) from exc


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
    if isinstance(last_error, ProviderResponseError):
        raise ProviderResponseError(
            f"{label} failed: {last_error}",
            debug_info=last_error.debug_info,
        ) from last_error
    if isinstance(last_error, httpx.HTTPStatusError):
        raise RuntimeError(
            f"{label} failed: HTTP {last_error.response.status_code}: "
            f"{_http_error_excerpt(last_error)}"
        ) from last_error
    raise RuntimeError(f"{label} failed: {last_error}") from last_error


async def generate_taste_profile(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    debug_info: dict[str, Any] | None = None,
    provider: str = "opus",
) -> dict[str, Any]:
    prompt = build_taste_profile_prompt(snapshot)
    if provider == "opus":
        model_name = ANTHROPIC_MODEL
        label = "Anthropic taste profile"
        raw, response_debug = await with_retry(
            lambda: call_anthropic_json(client, api_key, prompt, 1800),
            label,
        )
    elif provider == "gpt45":
        model_name = OPENAI_MODEL
        label = "OpenAI taste profile"
        raw, response_debug = await with_retry(
            lambda: call_openai_json(client, api_key, prompt, 1800),
            label,
        )
    else:
        raise ValueError(f"Unsupported taste profile provider: {provider}")

    response_debug["provider"] = provider
    if debug_info is not None:
        debug_info.update(response_debug)
    try:
        normalized = normalize_taste_profile(raw)
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=response_debug) from exc
    normalized["model"] = model_name
    normalized["provider"] = provider
    return normalized


async def generate_anthropic_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
    debug_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw, response_debug = await with_retry(
        lambda: call_anthropic_json(client, api_key, build_recommendations_prompt(snapshot), 2600),
        "Anthropic recommendations",
    )
    if debug_info is not None:
        debug_info.update(response_debug)
    try:
        normalized = normalize_recommendations(raw, existing_books)
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=response_debug) from exc
    normalized["model"] = ANTHROPIC_MODEL
    return normalized


async def generate_openai_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
    debug_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw, response_debug = await with_retry(
        lambda: call_openai_json(client, api_key, build_recommendations_prompt(snapshot), 2600),
        "OpenAI recommendations",
    )
    if debug_info is not None:
        debug_info.update(response_debug)
    try:
        normalized = normalize_recommendations(raw, existing_books)
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=response_debug) from exc
    normalized["model"] = OPENAI_MODEL
    return normalized


async def generate_gemini_recommendations(
    client: httpx.AsyncClient,
    snapshot: dict[str, Any],
    api_key: str,
    existing_books: set[tuple[str, str]],
    debug_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw, response_debug = await with_retry(
        lambda: call_gemini_json(
            client,
            api_key,
            build_recommendations_prompt(snapshot),
            GEMINI_MAX_OUTPUT_TOKENS,
        ),
        "Gemini recommendations",
    )
    if debug_info is not None:
        debug_info.update(response_debug)
    try:
        normalized = normalize_recommendations(raw, existing_books)
    except Exception as exc:  # noqa: BLE001
        raise ProviderResponseError(str(exc), debug_info=response_debug) from exc
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


def normalize_taste_profile_provider(raw_value: str | None) -> str:
    provider = (raw_value or os.getenv("TASTE_PROFILE_PROVIDER") or "claude").strip().lower()
    mapped = TASTE_PROFILE_PROVIDER_ALIASES.get(provider)
    if mapped is None:
        valid = ", ".join(sorted(TASTE_PROFILE_PROVIDER_ALIASES))
        raise ValueError(f"Unknown taste profile provider '{provider}'. Valid values: {valid}")
    return mapped


def _taste_profile_runtime_model(provider: str) -> str:
    if provider == "opus":
        return ANTHROPIC_MODEL
    if provider == "gpt45":
        return OPENAI_MODEL
    raise ValueError(f"Unsupported taste profile provider: {provider}")


def skip_generation(
    cache_payload: dict[str, Any],
    llm_input_hash: str,
    force: bool,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool | None = None,
    taste_profile_provider: str | None = None,
) -> bool:
    if refresh_taste_profile is None:
        refresh_taste_profile = selected_providers is None

    if force:
        return False

    target_input_hashes = _target_input_hashes(cache_payload)
    recommendations = cache_payload.get("recommendations") or {}
    cache_dry_run = bool(cache_payload.get("dry_run"))
    prompt_hash = cache_payload.get("prompt_hash")
    if cache_dry_run != LLM_DRY_RUN or prompt_hash != compute_prompt_hash():
        return False

    if refresh_taste_profile:
        taste_profile_provider = normalize_taste_profile_provider(taste_profile_provider)
        taste_profile = cache_payload.get("taste_profile") or {}
        expected_taste_model = _taste_profile_runtime_model(taste_profile_provider)
        cached_taste_model = str(taste_profile.get("model") or "")
        if (
            target_input_hashes["taste_profile"] != llm_input_hash
            or taste_profile.get("error")
            or not taste_profile.get("summary")
            or (
                (taste_profile_provider != "opus" or bool(cached_taste_model))
                and cached_taste_model != expected_taste_model
            )
        ):
            return False

    runtime_models = {
        "opus": ANTHROPIC_MODEL,
        "gpt45": OPENAI_MODEL,
        "gemini": GEMINI_MODEL,
    }
    for provider in _selected_targets(selected_providers, refresh_taste_profile):
        if provider == "taste_profile":
            continue
        entry = recommendations.get(provider) or {}
        if target_input_hashes[provider] != llm_input_hash:
            return False
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
    taste_profile_provider: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if refresh_taste_profile is None:
        refresh_taste_profile = selected_providers is None
    taste_profile_provider = (
        normalize_taste_profile_provider(taste_profile_provider)
        if refresh_taste_profile
        else "opus"
    )

    full_refresh = _is_full_refresh(selected_providers, refresh_taste_profile)
    llm_input_hash = compute_llm_input_hash(books_payload)
    if skip_generation(
        cache_payload,
        llm_input_hash,
        force,
        selected_providers=selected_providers,
        refresh_taste_profile=refresh_taste_profile,
        taste_profile_provider=taste_profile_provider,
    ):
        return cache_payload, True

    snapshot = build_library_snapshot(books_payload)
    taste_profile_snapshot = build_taste_profile_snapshot(books_payload)
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

    result = merge_defaults(default_llm_cache(), cache_payload)
    current_generated_at = utc_now_iso()
    target_input_hashes = {
        **default_target_input_hashes(),
        **_target_input_hashes(cache_payload),
    }
    target_generated_at = {
        **default_target_generated_at(),
        **_target_generated_at(cache_payload),
    }
    result["target_input_hashes"] = target_input_hashes
    result["target_generated_at"] = target_generated_at
    runtime_models = {
        "opus": ANTHROPIC_MODEL,
        "gpt45": OPENAI_MODEL,
        "gemini": GEMINI_MODEL,
    }
    taste_profile_model = _taste_profile_runtime_model(taste_profile_provider)
    successful_targets: set[str] = set()

    def mark_target_success(target: str) -> None:
        target_input_hashes[target] = llm_input_hash
        target_generated_at[target] = current_generated_at
        successful_targets.add(target)

    def finalize_metadata() -> None:
        if not successful_targets:
            return
        result["books_hash"] = llm_input_hash
        result["llm_input_hash"] = llm_input_hash
        result["generated_at"] = current_generated_at
        result["dry_run"] = LLM_DRY_RUN
        result["prompt_hash"] = compute_prompt_hash()
        result["partial_refresh"] = not full_refresh

    if LLM_DRY_RUN:
        if refresh_taste_profile:
            result["taste_profile"] = build_mock_taste_profile(
                taste_profile_model,
                taste_profile_provider,
            )
            mark_target_success("taste_profile")
        if "opus" in target_providers:
            result["recommendations"]["opus"] = build_mock_recommendations(
                ANTHROPIC_MODEL, "Anthropic"
            )
            mark_target_success("opus")
        if "gpt45" in target_providers:
            result["recommendations"]["gpt45"] = build_mock_recommendations(
                OPENAI_MODEL, "OpenAI"
            )
            mark_target_success("gpt45")
        if "gemini" in target_providers:
            result["recommendations"]["gemini"] = build_mock_recommendations(
                GEMINI_MODEL, "Gemini"
            )
            mark_target_success("gemini")
        finalize_metadata()
        return result, False

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if refresh_taste_profile:
            taste_profile_debug: dict[str, Any] = {
                "model": taste_profile_model,
                "provider": taste_profile_provider,
            }
            taste_profile_keys = {
                "opus": (anthropic_key, "ANTHROPIC_API_KEY is not set."),
                "gpt45": (openai_key, "OPENAI_API_KEY is not set."),
            }
            taste_api_key, taste_missing_message = taste_profile_keys[taste_profile_provider]
            if taste_api_key:
                try:
                    result["taste_profile"] = await generate_taste_profile(
                        client,
                        taste_profile_snapshot,
                        taste_api_key,
                        debug_info=taste_profile_debug,
                        provider=taste_profile_provider,
                    )
                    mark_target_success("taste_profile")
                except Exception as exc:  # noqa: BLE001
                    if full_refresh:
                        result["taste_profile"] = {
                            "error": str(exc),
                            "model": taste_profile_model,
                            "provider": taste_profile_provider,
                        }
                    taste_profile_debug["error"] = str(exc)
                    if isinstance(exc, ProviderResponseError):
                        taste_profile_debug.update(exc.debug_info)
            else:
                if full_refresh:
                    result["taste_profile"] = {
                        "error": taste_missing_message,
                        "model": taste_profile_model,
                        "provider": taste_profile_provider,
                    }
                taste_profile_debug["error"] = taste_missing_message
            result["debug"]["taste_profile"] = taste_profile_debug

        recommendation_tasks: list[tuple[str, asyncio.Future | Any | None, str]] = []
        recommendation_debug: dict[str, dict[str, Any]] = {
            provider: {"model": runtime_models[provider]}
            for provider in target_providers
        }
        provider_factories = {
            "opus": (
                anthropic_key,
                lambda: generate_anthropic_recommendations(
                    client,
                    snapshot,
                    anthropic_key,
                    all_books,
                    debug_info=recommendation_debug["opus"],
                ),
                "ANTHROPIC_API_KEY is not set.",
            ),
            "gpt45": (
                openai_key,
                lambda: generate_openai_recommendations(
                    client,
                    snapshot,
                    openai_key,
                    all_books,
                    debug_info=recommendation_debug["gpt45"],
                ),
                "OPENAI_API_KEY is not set.",
            ),
            "gemini": (
                gemini_key,
                lambda: generate_gemini_recommendations(
                    client,
                    snapshot,
                    gemini_key,
                    all_books,
                    debug_info=recommendation_debug["gemini"],
                ),
                "GEMINI_API_KEY or GOOGLE_API_KEY is not set.",
            ),
        }

        for provider in target_providers:
            api_key, factory, missing_message = provider_factories[provider]
            if api_key:
                recommendation_tasks.append((provider, factory(), runtime_models[provider]))
            else:
                if full_refresh:
                    result["recommendations"][provider] = {
                        "model": runtime_models[provider],
                        "error": missing_message,
                    }
                recommendation_debug[provider]["error"] = missing_message
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
                if full_refresh:
                    result["recommendations"][provider_key] = {
                        "model": model_name,
                        "error": str(response),
                    }
                recommendation_debug[provider_key]["error"] = str(response)
                if isinstance(response, ProviderResponseError):
                    recommendation_debug[provider_key].update(response.debug_info)
            else:
                result["recommendations"][provider_key] = response
                mark_target_success(provider_key)
            result["debug"]["recommendations"][provider_key] = recommendation_debug[provider_key]

    finalize_metadata()
    return result, False


def _save_llm_cache_to_db(conn: Any, payload: dict[str, Any]) -> None:
    """Write LLM cache payload to SQLite llm_cache table as separate keys."""
    from db import set_llm_cache_value

    set_llm_cache_value(conn, "metadata", {
        "books_hash": payload.get("books_hash", ""),
        "llm_input_hash": payload.get("llm_input_hash") or payload.get("books_hash", ""),
        "generated_at": payload.get("generated_at"),
        "dry_run": payload.get("dry_run", False),
        "prompt_hash": payload.get("prompt_hash", ""),
        "partial_refresh": payload.get("partial_refresh", False),
        "target_input_hashes": payload.get("target_input_hashes", {}),
        "target_generated_at": payload.get("target_generated_at", {}),
    })
    set_llm_cache_value(conn, "debug", payload.get("debug", {}))
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
        help="When using --provider, also refresh the taste profile.",
    )
    parser.add_argument(
        "--taste-profile-provider",
        default=None,
        help="Taste profile provider to use: claude or openai. Defaults to TASTE_PROFILE_PROVIDER or claude.",
    )
    args = parser.parse_args()
    try:
        args.providers = normalize_provider_selection(args.provider)
        if args.taste_profile_provider is not None:
            args.taste_profile_provider = normalize_taste_profile_provider(
                args.taste_profile_provider
            )
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
            taste_profile_provider=args.taste_profile_provider,
        )
    return _main_json(
        Path(args.books),
        Path(args.cache),
        force=args.force,
        selected_providers=args.providers,
        refresh_taste_profile=args.with_taste_profile,
        taste_profile_provider=args.taste_profile_provider,
    )


def _main_sqlite(
    db_path: Path,
    force: bool,
    selected_providers: set[str] | None = None,
    refresh_taste_profile: bool = False,
    taste_profile_provider: str | None = None,
) -> int:
    from bookshelf_data import BookshelfDB

    store = BookshelfDB(db_path)
    books_payload = store.books(include_notes=True)
    cache_payload = store.llm_cache()

    generated_payload, skipped = asyncio.run(
        generate_cache_payload(
            books_payload,
            cache_payload,
            force=force,
            selected_providers=selected_providers,
            refresh_taste_profile=refresh_taste_profile,
            taste_profile_provider=taste_profile_provider,
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
    taste_profile_provider: str | None = None,
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
            taste_profile_provider=taste_profile_provider,
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
