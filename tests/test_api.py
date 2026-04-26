import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
                        "gemini": {
                            "model": "gemini-test",
                            "books": [{"title": "Rec G", "author": "Author G", "reason": "Specific Gemini.", "confidence": "medium"}],
                            "reasoning": "Gemini strategy",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        self.original_env = os.environ.copy()
        os.environ["DB_PATH"] = ""
        os.environ["BOOKSHELF_AUTH_TOKEN"] = ""
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
        self.assertIn("gemini", payload)
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
        self.assertTrue(payload["llm_outdated"])
        self.assertEqual(payload["llm_targets"]["taste_profile"]["generated_at"], "2026-03-22T13:00:00Z")
        self.assertTrue(payload["llm_targets"]["taste_profile"]["has_content"])
        self.assertTrue(payload["llm_targets"]["gpt45"]["outdated"])
        self.assertTrue(payload["llm_targets"]["opus"]["has_content"])

    def test_activity_endpoint_returns_empty_on_json_backend(self):
        response = self.client.get("/api/activity")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["pagination"]["has_more"])

    def test_sync_endpoint_returns_410_gone(self):
        response = self.client.post("/api/sync")
        self.assertEqual(response.status_code, 410)

    def test_llm_status_endpoint(self):
        response = self.client.get("/api/llm-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "idle")

    def test_llm_regenerate_requires_sqlite(self):
        response = self.client.post(
            "/api/llm/regenerate",
            headers={"Authorization": "Bearer fake"},
        )
        # Without BOOKSHELF_AUTH_TOKEN set, returns 503
        self.assertIn(response.status_code, [400, 503])

    def test_crud_requires_sqlite(self):
        """CRUD endpoints return 400 or 401 on JSON backend."""
        os.environ["BOOKSHELF_AUTH_TOKEN"] = "test-token"
        if "api.main" in sys.modules:
            importlib.reload(sys.modules["api.main"])
        client = TestClient(sys.modules["api.main"].app)
        headers = {"Authorization": "Bearer test-token"}

        resp = client.post("/api/books", json={"title": "X", "author": "Y"}, headers=headers)
        self.assertEqual(resp.status_code, 400)

        resp = client.put("/api/books/1", json={"title": "X"}, headers=headers)
        self.assertEqual(resp.status_code, 400)

        resp = client.delete("/api/books/1", headers=headers)
        self.assertEqual(resp.status_code, 400)


@unittest.skipIf(TestClient is None, "fastapi is not installed")
class SqliteApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "bookshelf.db"
        self.original_env = os.environ.copy()
        os.environ["DB_PATH"] = str(self.db_path)
        os.environ["BOOKSHELF_AUTH_TOKEN"] = "test-token"
        os.environ["BOOKSHELF_CORS_ORIGINS"] = "https://book.tanxy.net"

        from db import get_connection, insert_book, run_migrations

        conn = get_connection(self.db_path)
        run_migrations(conn)
        insert_book(
            conn,
            {
                "title": "Book A",
                "author": "Author One",
                "my_rating": 5,
                "date_read": "2026-03-10",
                "date_added": "2026-03-10",
                "shelves": ["history"],
                "exclusive_shelf": "read",
                "review": "Loved it.",
            },
        )
        conn.commit()
        conn.close()

        if "api.main" in sys.modules:
            self.api_main = importlib.reload(sys.modules["api.main"])
        else:
            self.api_main = importlib.import_module("api.main")

        self.client = TestClient(self.api_main.app)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tempdir.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def test_llm_status_returns_targets_when_present(self):
        self.api_main._llm_status = {
            "status": "running",
            "targets": ["gpt45"],
            "started_at": "2026-03-22T15:00:00Z",
        }

        response = self.client.get("/api/llm-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["targets"], ["gpt45"])

    def test_llm_regenerate_accepts_target_list(self):
        captured: dict[str, object] = {}

        def fake_create_task(coro):
            captured["coro"] = coro
            return object()

        run_mock = AsyncMock()
        with (
            patch.object(self.api_main, "_run_llm_regeneration", run_mock),
            patch.object(self.api_main.asyncio, "create_task", side_effect=fake_create_task),
        ):
            response = self.client.post(
                "/api/llm/regenerate",
                headers=self._headers(),
                json={"force": False, "targets": ["gpt45"]},
            )
            self.assertIn("coro", captured)
            asyncio.run(captured["coro"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["targets"], ["gpt45"])
        run_mock.assert_awaited_once_with(
            force=False,
            targets=("gpt45",),
            taste_profile_provider=None,
        )

    def test_llm_regenerate_accepts_taste_profile_provider(self):
        captured: dict[str, object] = {}

        def fake_create_task(coro):
            captured["coro"] = coro
            return object()

        run_mock = AsyncMock()
        with (
            patch.object(self.api_main, "_run_llm_regeneration", run_mock),
            patch.object(self.api_main.asyncio, "create_task", side_effect=fake_create_task),
        ):
            response = self.client.post(
                "/api/llm/regenerate",
                headers=self._headers(),
                json={
                    "force": False,
                    "targets": ["taste_profile"],
                    "taste_profile_provider": "openai",
                },
            )
            self.assertIn("coro", captured)
            asyncio.run(captured["coro"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["targets"], ["taste_profile"])
        self.assertEqual(response.json()["taste_profile_provider"], "gpt45")
        run_mock.assert_awaited_once_with(
            force=False,
            targets=("taste_profile",),
            taste_profile_provider="gpt45",
        )

    def test_llm_regenerate_rejects_empty_or_unknown_targets(self):
        empty_response = self.client.post(
            "/api/llm/regenerate",
            headers=self._headers(),
            json={"targets": []},
        )
        invalid_response = self.client.post(
            "/api/llm/regenerate",
            headers=self._headers(),
            json={"targets": ["unknown-model"]},
        )

        self.assertEqual(empty_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
