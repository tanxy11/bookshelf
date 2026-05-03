import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("httpx", SimpleNamespace(AsyncClient=object, Timeout=object))

from bookshelf_data import compute_books_hash, compute_llm_input_hash, default_llm_cache, load_json  # noqa: E402
from scripts import generate_llm  # noqa: E402


def sample_books_payload() -> dict:
    return {
        "generated_at": "2026-03-22T12:00:00Z",
        "books": {
            "read": [
                {
                    "title": "Book A",
                    "author": "Author One",
                    "my_rating": 5,
                    "my_review": "Loved it.",
                    "shelves": ["history"],
                    "date_read": "2026-03-10",
                },
                {
                    "title": "Book B",
                    "author": "Author Two",
                    "my_rating": 4,
                    "my_review": None,
                    "shelves": ["fiction"],
                    "date_read": "2026-03-08",
                },
            ],
            "currently_reading": [],
            "to_read": [{"title": "Future Book", "author": "Future Author"}],
        },
        "stats": {"total_read": 2},
    }


class GenerateLlmTests(unittest.TestCase):
    def test_books_hash_ignores_read_order(self):
        books = sample_books_payload()["books"]["read"]
        reversed_books = list(reversed(books))
        self.assertEqual(compute_books_hash(books), compute_books_hash(reversed_books))

    def test_books_hash_changes_when_non_read_shelf_changes(self):
        books_payload = sample_books_payload()
        original_hash = compute_books_hash(books_payload)

        books_payload["books"]["currently_reading"].append(
            {"title": "Fresh Start", "author": "New Author"}
        )

        self.assertNotEqual(original_hash, compute_books_hash(books_payload))

    def test_llm_input_hash_ignores_non_read_shelf_changes(self):
        books_payload = sample_books_payload()
        original_hash = compute_llm_input_hash(books_payload)

        books_payload["books"]["currently_reading"].append(
            {"title": "Fresh Start", "author": "New Author"}
        )
        books_payload["books"]["to_read"].append(
            {"title": "Future Two", "author": "Future Author Two"}
        )

        self.assertEqual(original_hash, compute_llm_input_hash(books_payload))

    def test_llm_input_hash_changes_when_review_changes(self):
        books_payload = sample_books_payload()
        original_hash = compute_llm_input_hash(books_payload)

        books_payload["books"]["read"][0]["my_review"] = "This landed differently."

        self.assertNotEqual(original_hash, compute_llm_input_hash(books_payload))

    def test_llm_input_hash_changes_when_notes_change(self):
        books_payload = sample_books_payload()
        original_hash = compute_llm_input_hash(books_payload)

        books_payload["books"]["currently_reading"].append(
            {
                "title": "Live Book",
                "author": "Author Three",
                "notes": [{"id": 1, "note_type": "thought", "content": "Fresh obsession."}],
            }
        )

        self.assertNotEqual(original_hash, compute_llm_input_hash(books_payload))

    def test_taste_profile_snapshot_uses_bucketed_input(self):
        books_payload = sample_books_payload()
        books_payload["books"]["read"] = [
            {
                "title": f"Book {index:02d}",
                "author": "Author",
                "my_rating": 5 if index == 55 else 3,
                "my_review": "Long review. " * (80 if index == 56 else 1),
                "shelves": ["history"],
                "date_read": f"2026-02-{(index % 28) + 1:02d}",
                "date_added": f"2026-01-{(index % 28) + 1:02d}",
                "read_events": [{"finished_on": f"2026-02-{(index % 28) + 1:02d}"}],
                "note_count": 1 if index == 57 else 0,
                "notes": [{"id": 9, "note_type": "thought", "content": "Older live wire."}]
                if index == 57
                else [],
            }
            for index in range(60)
        ]
        books_payload["books"]["currently_reading"] = [
            {
                "title": "In Progress With Notes",
                "author": "Reader",
                "notes": [
                    {"id": 2, "note_type": "question", "content": "What is changing?"},
                    {"id": 1, "note_type": "thought", "content": "Something is brewing."},
                    {"id": 0, "note_type": "thought", "content": "Older note."},
                ],
                "note_count": 3,
            },
            {"title": "In Progress Without Notes", "author": "Reader", "notes": []},
        ]

        snapshot = generate_llm.build_taste_profile_snapshot(books_payload)

        self.assertEqual(len(snapshot["recent_read_books"]), 50)
        self.assertEqual(
            snapshot["currently_reading_with_notes"][0]["title"],
            "In Progress With Notes",
        )
        self.assertEqual(len(snapshot["currently_reading_with_notes"][0]["notes"]), 2)
        self.assertEqual(snapshot["currently_reading_with_notes"][0]["evidence_status"], "in_progress")
        self.assertGreaterEqual(len(snapshot["historical_anchors"]), 1)
        self.assertIn("selection_strategy", snapshot)
        self.assertEqual(snapshot["excluded_counts"]["currently_reading_without_notes"], 1)

    def test_recommendations_snapshot_omits_to_read_and_prioritizes_current_context(self):
        books_payload = sample_books_payload()
        books_payload["books"]["read"] = [
            {
                "title": f"Book {index:02d}",
                "author": "Author",
                "my_rating": 5 if index == 45 else 3,
                "my_review": "Long review. " * (80 if index == 46 else 1),
                "shelves": ["history"],
                "date_read": f"2026-02-{(index % 28) + 1:02d}",
                "date_added": f"2026-01-{(index % 28) + 1:02d}",
                "read_events": [{"finished_on": f"2026-02-{(index % 28) + 1:02d}"}],
                "note_count": 0,
                "notes": [],
            }
            for index in range(60)
        ]
        books_payload["books"]["currently_reading"] = [
            {
                "title": "Live Wire",
                "author": "Reader",
                "notes": [
                    {"id": 2, "note_type": "question", "content": "What should come next?"},
                    {"id": 1, "note_type": "thought", "content": "This feels urgent."},
                ],
                "note_count": 2,
            },
            {"title": "No Notes Yet", "author": "Reader", "notes": []},
        ]
        books_payload["books"]["to_read"] = [
            {"title": "Known Unknown", "author": "Future Author"},
        ]

        snapshot = generate_llm.build_recommendations_snapshot(books_payload)
        serialized = json_dump(snapshot)

        self.assertEqual(len(snapshot["recent_read_books"]), 40)
        self.assertEqual(snapshot["currently_reading_with_notes"][0]["title"], "Live Wire")
        self.assertNotIn("to_read", snapshot)
        self.assertNotIn("Known Unknown", serialized)
        self.assertEqual(snapshot["excluded_counts"]["to_read_books_omitted"], 1)

    def test_recommendation_normalization_marks_to_read_matches_after_generation(self):
        raw = {
            "reasoning": "A strategy.",
            "books": [
                {
                    "title": "Known Unknown",
                    "author": "Future Author",
                    "reason": "Specific reason.",
                    "confidence": "high",
                    "from_to_read": False,
                }
            ],
        }

        normalized = generate_llm.normalize_recommendations(
            raw,
            existing_books=set(),
            to_read_books={("known unknown", "future author")},
        )

        self.assertTrue(normalized["books"][0]["from_to_read"])

    def test_skip_generation_when_hash_matches(self):
        books_payload = sample_books_payload()
        books_hash = compute_llm_input_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["llm_input_hash"] = books_hash
        cache_payload["prompt_hash"] = generate_llm.compute_prompt_hash()
        cache_payload["taste_profile"] = {
            "summary": "Summary",
            "traits": [{"label": "Trait", "explanation": "Explanation"}],
            "blind_spots": "Blind spots",
        }
        cache_payload["recommendations"]["opus"]["books"] = [{"title": "Rec A", "author": "Author A", "reason": "Specific.", "confidence": "high"}]
        cache_payload["recommendations"]["opus"]["reasoning"] = "Strategy"
        cache_payload["recommendations"]["opus"]["model"] = generate_llm.ANTHROPIC_MODEL
        cache_payload["recommendations"]["gpt45"]["books"] = [{"title": "Rec B", "author": "Author B", "reason": "Specific.", "confidence": "medium"}]
        cache_payload["recommendations"]["gpt45"]["reasoning"] = "Strategy"
        cache_payload["recommendations"]["gpt45"]["model"] = generate_llm.OPENAI_MODEL
        cache_payload["recommendations"]["gemini"]["books"] = [{"title": "Rec C", "author": "Author C", "reason": "Specific.", "confidence": "medium"}]
        cache_payload["recommendations"]["gemini"]["reasoning"] = "Strategy"
        cache_payload["recommendations"]["gemini"]["model"] = generate_llm.GEMINI_MODEL
        self.assertTrue(generate_llm.skip_generation(cache_payload, books_hash, force=False))
        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=True))

    def test_skip_generation_rebuilds_when_runtime_config_changes(self):
        books_payload = sample_books_payload()
        books_hash = compute_llm_input_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["llm_input_hash"] = books_hash
        cache_payload["prompt_hash"] = generate_llm.compute_prompt_hash()
        cache_payload["recommendations"]["opus"]["model"] = "old-anthropic-model"
        cache_payload["recommendations"]["gpt45"]["model"] = "old-openai-model"
        cache_payload["recommendations"]["gemini"]["model"] = "old-gemini-model"

        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=False))

    def test_skip_generation_rebuilds_when_prompt_changes(self):
        books_payload = sample_books_payload()
        books_hash = compute_llm_input_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["llm_input_hash"] = books_hash
        cache_payload["prompt_hash"] = "old-prompt-hash"
        cache_payload["recommendations"]["opus"]["model"] = generate_llm.ANTHROPIC_MODEL
        cache_payload["recommendations"]["gpt45"]["model"] = generate_llm.OPENAI_MODEL
        cache_payload["recommendations"]["gemini"]["model"] = generate_llm.GEMINI_MODEL

        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=False))

    def test_skip_generation_partial_provider_rebuilds_after_provider_error(self):
        books_payload = sample_books_payload()
        books_hash = compute_llm_input_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["llm_input_hash"] = books_hash
        cache_payload["prompt_hash"] = generate_llm.compute_prompt_hash()
        cache_payload["recommendations"]["gemini"] = {
            "model": generate_llm.GEMINI_MODEL,
            "error": "previous failure",
        }

        self.assertFalse(
            generate_llm.skip_generation(
                cache_payload,
                books_hash,
                force=False,
                selected_providers={"gemini"},
                refresh_taste_profile=False,
            )
        )

    def test_partial_success_writes_cache_payload(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "test-key",
                    "OPENAI_API_KEY": "test-key",
                    "GEMINI_API_KEY": "test-key",
                },
                clear=False,
            ),
            patch.object(generate_llm.httpx, "Timeout", return_value=None),
            patch.object(generate_llm.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(
                generate_llm,
                "generate_taste_profile",
                AsyncMock(return_value={"summary": "Sharp.", "traits": [{"label": "Trait", "explanation": "Explained."}], "blind_spots": "More poetry."}),
            ),
            patch.object(
                generate_llm,
                "generate_anthropic_recommendations",
                AsyncMock(side_effect=RuntimeError("anthropic failed")),
            ),
            patch.object(
                generate_llm,
                "generate_openai_recommendations",
                AsyncMock(return_value={"model": "gpt-test", "books": [{"title": "Rec", "author": "Author", "reason": "Specific.", "confidence": "high"}], "reasoning": "Pattern-driven."}),
            ),
            patch.object(
                generate_llm,
                "generate_gemini_recommendations",
                AsyncMock(return_value={"model": "gemini-test", "books": [{"title": "Rec 2", "author": "Author 2", "reason": "Specific 2.", "confidence": "medium"}], "reasoning": "Another pattern-driven view."}),
            ),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(books_payload, cache_payload, force=False)
            )

        self.assertFalse(skipped)
        self.assertEqual(payload["recommendations"]["gpt45"]["model"], "gpt-test")
        self.assertEqual(payload["recommendations"]["gemini"]["model"], "gemini-test")
        self.assertIn("error", payload["recommendations"]["opus"])
        self.assertEqual(payload["taste_profile"]["summary"], "Sharp.")
        self.assertFalse(payload["dry_run"])
        llm_input_hash = compute_llm_input_hash(books_payload)
        self.assertEqual(payload["target_input_hashes"]["taste_profile"], llm_input_hash)
        self.assertEqual(payload["target_input_hashes"]["gpt45"], llm_input_hash)
        self.assertEqual(payload["target_input_hashes"]["gemini"], llm_input_hash)
        self.assertEqual(payload["target_input_hashes"]["opus"], "")

    def test_call_gemini_json_uses_structured_output_schema(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "usageMetadata": {
                        "promptTokenCount": 123,
                        "candidatesTokenCount": 45,
                        "totalTokenCount": 168,
                    },
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json_dump(
                                            {
                                                "reasoning": "Pattern-driven.",
                                                "books": [
                                                    {
                                                        "title": "Rec",
                                                        "author": "Author",
                                                        "reason": "Specific.",
                                                        "confidence": "high",
                                                        "from_to_read": False,
                                                    }
                                                ],
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        payload, debug_info = asyncio.run(
            generate_llm.call_gemini_json(FakeClient(), "test-key", "prompt", 2600)
        )

        self.assertEqual(payload["books"][0]["title"], "Rec")
        self.assertEqual(debug_info["model"], generate_llm.GEMINI_MODEL)
        self.assertEqual(debug_info["usage_metadata"]["promptTokenCount"], 123)
        self.assertIn(":generateContent", captured["url"])
        generation_config = captured["json"]["generationConfig"]
        self.assertEqual(generation_config["responseMimeType"], "application/json")
        self.assertEqual(generation_config["thinkingConfig"]["thinkingLevel"], "medium")
        self.assertEqual(generation_config["maxOutputTokens"], 2600)
        self.assertEqual(
            generation_config["responseJsonSchema"],
            generate_llm.GEMINI_RECOMMENDATIONS_JSON_SCHEMA,
        )

    def test_call_openai_json_omits_temperature_for_reasoning_models(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json_dump({"ok": True}),
                            }
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        payload, debug_info = asyncio.run(
            generate_llm.call_openai_json(FakeClient(), "test-key", "prompt", 80)
        )

        self.assertEqual(payload["ok"], True)
        self.assertNotIn("temperature", captured["json"])
        self.assertEqual(captured["json"]["max_completion_tokens"], 80)
        self.assertEqual(debug_info["model"], generate_llm.OPENAI_MODEL)

    def test_generate_taste_profile_can_use_openai_provider(self):
        raw_profile = {
            "summary": "A lively profile.",
            "traits": [{"label": "Trait", "explanation": "Explained."}],
            "blind_spots": "More jokes.",
        }
        openai_mock = AsyncMock(return_value=(raw_profile, {"model": "gpt-test"}))

        with (
            patch.object(generate_llm, "OPENAI_MODEL", "gpt-test"),
            patch.object(generate_llm, "call_openai_json", openai_mock),
        ):
            profile = asyncio.run(
                generate_llm.generate_taste_profile(
                    object(),
                    sample_books_payload(),
                    "test-key",
                    provider="gpt45",
                )
            )

        self.assertEqual(profile["summary"], "A lively profile.")
        self.assertEqual(profile["model"], "gpt-test")
        self.assertEqual(profile["provider"], "gpt45")
        openai_mock.assert_awaited_once()

    def test_provider_error_records_debug_info_without_overwriting_cached_content(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()
        cache_payload["recommendations"]["gemini"] = {
            "model": "existing-gemini",
            "books": [{"title": "Cached Rec", "author": "Author", "reason": "Keep this.", "confidence": "medium"}],
            "reasoning": "Cached reasoning",
        }
        cache_payload["target_input_hashes"]["gemini"] = "stale-hash"
        cache_payload["target_generated_at"]["gemini"] = "2026-03-20T09:00:00Z"

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
            patch.object(generate_llm.httpx, "Timeout", return_value=None),
            patch.object(generate_llm.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(
                generate_llm,
                "generate_gemini_recommendations",
                AsyncMock(
                    side_effect=generate_llm.ProviderResponseError(
                        "Gemini recommendations failed: bad json",
                        debug_info={
                            "model": "gemini-test",
                            "raw_text_excerpt": '{"reasoning": "cut off',
                            "finish_reason": "MAX_TOKENS",
                            "usage_metadata": {"promptTokenCount": 123638},
                        },
                    )
                ),
            ),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(
                    books_payload,
                    cache_payload,
                    force=False,
                    selected_providers={"gemini"},
                    refresh_taste_profile=False,
                )
            )

        self.assertFalse(skipped)
        self.assertEqual(payload["recommendations"]["gemini"]["model"], "existing-gemini")
        self.assertEqual(
            payload["debug"]["recommendations"]["gemini"]["raw_text_excerpt"],
            '{"reasoning": "cut off',
        )
        self.assertEqual(
            payload["debug"]["recommendations"]["gemini"]["finish_reason"],
            "MAX_TOKENS",
        )
        self.assertEqual(
            payload["debug"]["recommendations"]["gemini"]["usage_metadata"]["promptTokenCount"],
            123638,
        )
        self.assertEqual(payload["target_input_hashes"]["gemini"], "stale-hash")
        self.assertEqual(payload["target_generated_at"]["gemini"], "2026-03-20T09:00:00Z")

    def test_partial_provider_refresh_preserves_other_cached_results(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()
        stale_hash = "stale-hash"
        stale_generated_at = "2026-03-20T10:00:00Z"
        cache_payload["taste_profile"] = {
            "summary": "Keep me",
            "traits": [{"label": "Trait", "explanation": "Explained."}],
            "blind_spots": "None",
        }
        cache_payload["recommendations"]["opus"] = {
            "model": "existing-claude",
            "books": [{"title": "Claude Rec", "author": "Author", "reason": "Reason", "confidence": "high"}],
            "reasoning": "Claude reasoning",
        }
        cache_payload["recommendations"]["gpt45"] = {
            "model": "existing-gpt",
            "books": [{"title": "GPT Rec", "author": "Author", "reason": "Reason", "confidence": "medium"}],
            "reasoning": "GPT reasoning",
        }
        cache_payload["target_input_hashes"] = {
            "taste_profile": stale_hash,
            "opus": stale_hash,
            "gpt45": stale_hash,
            "gemini": stale_hash,
        }
        cache_payload["target_generated_at"] = {
            "taste_profile": stale_generated_at,
            "opus": stale_generated_at,
            "gpt45": stale_generated_at,
            "gemini": stale_generated_at,
        }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
            patch.object(generate_llm.httpx, "Timeout", return_value=None),
            patch.object(generate_llm.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(
                generate_llm,
                "generate_gemini_recommendations",
                AsyncMock(return_value={"model": "gemini-test", "books": [{"title": "Gemini Rec", "author": "Author 2", "reason": "Specific 2.", "confidence": "medium"}], "reasoning": "Gemini reasoning"}),
            ),
            patch.object(
                generate_llm,
                "generate_taste_profile",
                AsyncMock(side_effect=AssertionError("should not refresh taste profile")),
            ),
            patch.object(
                generate_llm,
                "generate_anthropic_recommendations",
                AsyncMock(side_effect=AssertionError("should not refresh claude")),
            ),
            patch.object(
                generate_llm,
                "generate_openai_recommendations",
                AsyncMock(side_effect=AssertionError("should not refresh openai")),
            ),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(
                    books_payload,
                    cache_payload,
                    force=False,
                    selected_providers={"gemini"},
                    refresh_taste_profile=False,
                )
            )

        self.assertFalse(skipped)
        self.assertEqual(payload["taste_profile"]["summary"], "Keep me")
        self.assertTrue(payload["partial_refresh"])
        self.assertEqual(payload["recommendations"]["opus"]["model"], "existing-claude")
        self.assertEqual(payload["recommendations"]["gpt45"]["model"], "existing-gpt")
        self.assertEqual(payload["recommendations"]["gemini"]["model"], "gemini-test")
        llm_input_hash = compute_llm_input_hash(books_payload)
        self.assertEqual(payload["target_input_hashes"]["taste_profile"], stale_hash)
        self.assertEqual(payload["target_input_hashes"]["opus"], stale_hash)
        self.assertEqual(payload["target_input_hashes"]["gpt45"], stale_hash)
        self.assertEqual(payload["target_input_hashes"]["gemini"], llm_input_hash)
        self.assertEqual(payload["target_generated_at"]["taste_profile"], stale_generated_at)
        self.assertEqual(payload["target_generated_at"]["opus"], stale_generated_at)
        self.assertEqual(payload["target_generated_at"]["gpt45"], stale_generated_at)
        self.assertEqual(payload["target_generated_at"]["gemini"], payload["generated_at"])

    def test_taste_profile_only_refresh_calls_only_taste_profile(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        taste_mock = AsyncMock(
            return_value={
                "summary": "Sharp.",
                "traits": [{"label": "Trait", "explanation": "Explained."}],
                "blind_spots": "More poetry.",
            }
        )

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False),
            patch.object(generate_llm.httpx, "Timeout", return_value=None),
            patch.object(generate_llm.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(generate_llm, "generate_taste_profile", taste_mock),
            patch.object(
                generate_llm,
                "generate_anthropic_recommendations",
                AsyncMock(side_effect=AssertionError("should not refresh claude recommendations")),
            ),
            patch.object(
                generate_llm,
                "generate_openai_recommendations",
                AsyncMock(side_effect=AssertionError("should not refresh openai recommendations")),
            ),
            patch.object(
                generate_llm,
                "generate_gemini_recommendations",
                AsyncMock(side_effect=AssertionError("should not refresh gemini recommendations")),
            ),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(
                    books_payload,
                    cache_payload,
                    force=False,
                    selected_providers=set(),
                    refresh_taste_profile=True,
                )
            )

        self.assertFalse(skipped)
        taste_mock.assert_awaited_once()
        self.assertEqual(payload["taste_profile"]["summary"], "Sharp.")
        self.assertTrue(payload["partial_refresh"])
        llm_input_hash = compute_llm_input_hash(books_payload)
        self.assertEqual(payload["target_input_hashes"]["taste_profile"], llm_input_hash)
        self.assertEqual(payload["target_input_hashes"]["opus"], "")
        self.assertEqual(payload["target_input_hashes"]["gpt45"], "")
        self.assertEqual(payload["target_input_hashes"]["gemini"], "")

    def test_gemini_provider_uses_shared_snapshot(self):
        books_payload = sample_books_payload()
        books_payload["books"]["to_read"] = [
            {"title": "Future A", "author": "Author A"},
            {"title": "Future B", "author": "Author B"},
        ]
        cache_payload = default_llm_cache()

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        gemini_mock = AsyncMock(
            return_value={
                "model": "gemini-test",
                "books": [
                    {
                        "title": "Gemini Rec",
                        "author": "Author 2",
                        "reason": "Specific 2.",
                        "confidence": "medium",
                    }
                ],
                "reasoning": "Gemini reasoning",
            }
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
            patch.object(generate_llm.httpx, "Timeout", return_value=None),
            patch.object(generate_llm.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(generate_llm, "generate_gemini_recommendations", gemini_mock),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(
                    books_payload,
                    cache_payload,
                    force=False,
                    selected_providers={"gemini"},
                    refresh_taste_profile=False,
                )
            )

        self.assertFalse(skipped)
        self.assertEqual(payload["recommendations"]["gemini"]["model"], "gemini-test")
        gemini_snapshot = gemini_mock.await_args.args[1]
        self.assertEqual(
            gemini_snapshot,
            generate_llm.build_recommendations_snapshot(books_payload),
        )

    def test_dry_run_skips_live_provider_calls(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()

        with (
            patch.object(generate_llm, "LLM_DRY_RUN", True),
            patch.object(generate_llm, "generate_taste_profile", AsyncMock(side_effect=AssertionError("should not call providers"))),
            patch.object(generate_llm, "generate_anthropic_recommendations", AsyncMock(side_effect=AssertionError("should not call providers"))),
            patch.object(generate_llm, "generate_openai_recommendations", AsyncMock(side_effect=AssertionError("should not call providers"))),
            patch.object(generate_llm, "generate_gemini_recommendations", AsyncMock(side_effect=AssertionError("should not call providers"))),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(books_payload, cache_payload, force=False)
            )

        self.assertFalse(skipped)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["taste_profile"]["summary"], "[DRY RUN] Mock taste profile")
        self.assertEqual(payload["recommendations"]["opus"]["books"][0]["title"], "[DRY RUN] Anthropic Pick 1")
        self.assertEqual(payload["recommendations"]["gpt45"]["books"][0]["title"], "[DRY RUN] OpenAI Pick 1")
        self.assertEqual(payload["recommendations"]["gemini"]["books"][0]["title"], "[DRY RUN] Gemini Pick 1")

    def test_main_persists_generated_cache(self):
        books_payload = sample_books_payload()

        with tempfile.TemporaryDirectory() as tmpdir:
            books_path = Path(tmpdir) / "books.json"
            cache_path = Path(tmpdir) / "llm_cache.json"
            books_path.write_text(json_dump(books_payload), encoding="utf-8")

            fake_payload = default_llm_cache()
            fake_payload["books_hash"] = "abc123"
            fake_payload["generated_at"] = "2026-03-22T12:30:00Z"
            fake_payload["taste_profile"] = {
                "summary": "Summary",
                "traits": [{"label": "Trait", "explanation": "Explanation"}],
                "blind_spots": "Blind spots",
            }
            fake_payload["recommendations"]["gpt45"] = {
                "model": "gpt-test",
                "books": [{"title": "Rec", "author": "Author", "reason": "Specific.", "confidence": "high"}],
                "reasoning": "Reasoning",
            }
            fake_payload["recommendations"]["gemini"] = {
                "model": "gemini-test",
                "books": [{"title": "Rec 2", "author": "Author 2", "reason": "Specific 2.", "confidence": "medium"}],
                "reasoning": "Reasoning 2",
            }

            with patch.object(
                generate_llm,
                "generate_cache_payload",
                AsyncMock(return_value=(fake_payload, False)),
            ):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "generate_llm.py",
                        "--books",
                        str(books_path),
                        "--cache",
                        str(cache_path),
                    ],
                ):
                    with patch.dict(os.environ, {"DB_PATH": ""}):
                        exit_code = generate_llm.main()

            self.assertEqual(exit_code, 0)
            saved = load_json(cache_path, default_llm_cache)
            self.assertEqual(saved["books_hash"], "abc123")
            self.assertEqual(saved["recommendations"]["gpt45"]["model"], "gpt-test")
            self.assertEqual(saved["recommendations"]["gemini"]["model"], "gemini-test")


    def test_main_sqlite_mode(self):
        """Test that --db flag routes to SQLite generation."""
        books_payload = sample_books_payload()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test SQLite DB
            from db import get_connection, run_migrations, insert_book, set_llm_cache_value

            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            run_migrations(conn)
            for book in books_payload["books"]["read"]:
                insert_book(conn, {
                    "title": book["title"],
                    "author": book["author"],
                    "my_rating": book.get("my_rating", 0),
                    "date_read": book.get("date_read") or None,
                    "date_added": "2026-01-01",
                    "shelves": book.get("shelves", []),
                    "exclusive_shelf": "read",
                    "review": book.get("my_review"),
                })
            conn.commit()

            fake_payload = default_llm_cache()
            fake_payload["books_hash"] = "xyz789"
            fake_payload["generated_at"] = "2026-03-22T14:00:00Z"
            fake_payload["taste_profile"] = {
                "summary": "DB Summary",
                "traits": [{"label": "Trait", "explanation": "Explanation"}],
                "blind_spots": "Blind spots",
            }
            fake_payload["recommendations"]["gpt45"] = {
                "model": "gpt-test",
                "books": [{"title": "Rec", "author": "Author", "reason": "Specific.", "confidence": "high"}],
                "reasoning": "Reasoning",
            }
            fake_payload["recommendations"]["gemini"] = {
                "model": "gemini-test",
                "books": [{"title": "Rec 2", "author": "Author 2", "reason": "Specific 2.", "confidence": "medium"}],
                "reasoning": "Reasoning 2",
            }

            with patch.object(
                generate_llm,
                "generate_cache_payload",
                AsyncMock(return_value=(fake_payload, False)),
            ):
                with patch.object(
                    sys,
                    "argv",
                    ["generate_llm.py", "--db", str(db_path)],
                ):
                    exit_code = generate_llm.main()

            self.assertEqual(exit_code, 0)

            # Verify results were written to the DB
            from db import get_llm_cache_value
            conn2 = get_connection(db_path)
            metadata = get_llm_cache_value(conn2, "metadata")
            self.assertEqual(metadata["books_hash"], "xyz789")
            tp = get_llm_cache_value(conn2, "taste_profile")
            self.assertEqual(tp["summary"], "DB Summary")
            conn2.close()
            conn.close()

    def test_parse_args_supports_partial_provider_refresh(self):
        with patch.object(
            sys,
            "argv",
            [
                "generate_llm.py",
                "--db",
                "data/bookshelf.db",
                "--provider",
                "gemini,chatgpt",
                "--with-taste-profile",
                "--taste-profile-provider",
                "openai",
            ],
        ):
            args = generate_llm.parse_args()

        self.assertEqual(args.providers, {"gemini", "gpt45"})
        self.assertTrue(args.with_taste_profile)
        self.assertEqual(args.taste_profile_provider, "gpt45")


def json_dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
