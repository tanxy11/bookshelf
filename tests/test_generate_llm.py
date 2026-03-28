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

from bookshelf_data import compute_books_hash, default_llm_cache, load_json  # noqa: E402
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

    def test_skip_generation_when_hash_matches(self):
        books_payload = sample_books_payload()
        books_hash = compute_books_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["prompt_hash"] = generate_llm.compute_prompt_hash()
        cache_payload["recommendations"]["opus"]["model"] = generate_llm.ANTHROPIC_MODEL
        cache_payload["recommendations"]["gpt45"]["model"] = generate_llm.OPENAI_MODEL
        self.assertTrue(generate_llm.skip_generation(cache_payload, books_hash, force=False))
        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=True))

    def test_skip_generation_rebuilds_when_runtime_config_changes(self):
        books_payload = sample_books_payload()
        books_hash = compute_books_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["prompt_hash"] = generate_llm.compute_prompt_hash()
        cache_payload["recommendations"]["opus"]["model"] = "old-anthropic-model"
        cache_payload["recommendations"]["gpt45"]["model"] = "old-openai-model"

        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=False))

    def test_skip_generation_rebuilds_when_prompt_changes(self):
        books_payload = sample_books_payload()
        books_hash = compute_books_hash(books_payload)
        cache_payload = default_llm_cache()
        cache_payload["books_hash"] = books_hash
        cache_payload["prompt_hash"] = "old-prompt-hash"
        cache_payload["recommendations"]["opus"]["model"] = generate_llm.ANTHROPIC_MODEL
        cache_payload["recommendations"]["gpt45"]["model"] = generate_llm.OPENAI_MODEL

        self.assertFalse(generate_llm.skip_generation(cache_payload, books_hash, force=False))

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
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "OPENAI_API_KEY": "test-key"}, clear=False),
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
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(books_payload, cache_payload, force=False)
            )

        self.assertFalse(skipped)
        self.assertEqual(payload["recommendations"]["gpt45"]["model"], "gpt-test")
        self.assertIn("error", payload["recommendations"]["opus"])
        self.assertEqual(payload["taste_profile"]["summary"], "Sharp.")
        self.assertFalse(payload["dry_run"])

    def test_dry_run_skips_live_provider_calls(self):
        books_payload = sample_books_payload()
        cache_payload = default_llm_cache()

        with (
            patch.object(generate_llm, "LLM_DRY_RUN", True),
            patch.object(generate_llm, "generate_taste_profile", AsyncMock(side_effect=AssertionError("should not call providers"))),
            patch.object(generate_llm, "generate_anthropic_recommendations", AsyncMock(side_effect=AssertionError("should not call providers"))),
            patch.object(generate_llm, "generate_openai_recommendations", AsyncMock(side_effect=AssertionError("should not call providers"))),
        ):
            payload, skipped = asyncio.run(
                generate_llm.generate_cache_payload(books_payload, cache_payload, force=False)
            )

        self.assertFalse(skipped)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["taste_profile"]["summary"], "[DRY RUN] Mock taste profile")
        self.assertEqual(payload["recommendations"]["opus"]["books"][0]["title"], "[DRY RUN] Anthropic Pick 1")
        self.assertEqual(payload["recommendations"]["gpt45"]["books"][0]["title"], "[DRY RUN] OpenAI Pick 1")

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


    def test_build_library_snapshot_includes_notes(self):
        books_payload = sample_books_payload()
        books_payload["books"]["read"][0]["notes"] = "Changed how I think about ecology."
        snapshot = generate_llm.build_library_snapshot(books_payload)
        self.assertEqual(snapshot["read"][0]["notes"], "Changed how I think about ecology.")
        # Book without notes should not have the key
        self.assertNotIn("notes", snapshot["read"][1])

    def test_build_library_snapshot_excludes_empty_notes(self):
        books_payload = sample_books_payload()
        books_payload["books"]["read"][0]["notes"] = ""
        snapshot = generate_llm.build_library_snapshot(books_payload)
        self.assertNotIn("notes", snapshot["read"][0])

    def test_taste_profile_prompt_mentions_notes(self):
        snapshot = generate_llm.build_library_snapshot(sample_books_payload())
        prompt = generate_llm.build_taste_profile_prompt(snapshot)
        self.assertIn("notes", prompt)
        self.assertIn("highest-signal", prompt)

    def test_recommendations_prompt_mentions_notes(self):
        snapshot = generate_llm.build_library_snapshot(sample_books_payload())
        prompt = generate_llm.build_recommendations_prompt(snapshot)
        self.assertIn("notes", prompt)
        self.assertIn("type of thinking", prompt)

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


def json_dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
