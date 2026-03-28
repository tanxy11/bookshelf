import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - local env may not have project deps installed
    TestClient = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@unittest.skipIf(TestClient is None, "fastapi is not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.books_path = Path(self.tempdir.name) / "books.json"
        self.llm_path = Path(self.tempdir.name) / "llm_cache.json"

        self.books_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-03-22T12:00:00Z",
                    "books": {
                        "read": [{"title": "Book A", "author": "Author One", "my_rating": 5}],
                        "currently_reading": [],
                        "to_read": [],
                    },
                    "stats": {"total_read": 1},
                }
            ),
            encoding="utf-8",
        )
        self.llm_path.write_text(
            json.dumps(
                {
                    "books_hash": "abc123",
                    "generated_at": "2026-03-22T13:00:00Z",
                    "taste_profile": {
                        "summary": "Summary",
                        "traits": [{"label": "Trait", "explanation": "Explanation"}],
                        "blind_spots": "Blind spots",
                    },
                    "recommendations": {
                        "opus": {
                            "model": "claude-test",
                            "books": [{"title": "Rec A", "author": "Author A", "reason": "Specific.", "confidence": "high"}],
                            "reasoning": "Strategy",
                        },
                        "gpt45": {"model": "gpt-test", "error": "temporarily unavailable"},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.original_env = os.environ.copy()
        os.environ["BOOKS_DATA"] = str(self.books_path)
        os.environ["LLM_CACHE_DATA"] = str(self.llm_path)
        os.environ["BOOKSHELF_CORS_ORIGINS"] = "https://book.tanxy.net"

        if "api.main" in sys.modules:
            self.api_main = importlib.reload(sys.modules["api.main"])
        else:
            self.api_main = importlib.import_module("api.main")

        self.client = TestClient(self.api_main.app)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tempdir.cleanup()

    def test_books_endpoint(self):
        response = self.client.get("/api/books")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["books"]["read"][0]["title"], "Book A")

    def test_taste_profile_endpoint(self):
        response = self.client.get("/api/taste-profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "Summary")

    def test_recommendations_endpoint(self):
        response = self.client.get("/api/recommendations")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("opus", payload)
        self.assertEqual(payload["opus"]["books"][0]["title"], "Rec A")

    def test_health_endpoint_prefers_llm_timestamp(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["generated_at"], "2026-03-22T13:00:00Z")
        self.assertEqual(payload["environment"], "production")
        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["has_taste_profile"])
        self.assertTrue(payload["has_recommendations"])

    def test_sync_endpoint_returns_410_gone(self):
        response = self.client.post("/api/sync")
        self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
