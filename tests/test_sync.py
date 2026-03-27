import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.modules.setdefault("httpx", SimpleNamespace(AsyncClient=object))

from sync import _dedupe_shelf, _merge, _sort_shelf


class MergeRegressionTests(unittest.TestCase):
    def test_new_insert_does_not_shift_later_match_to_wrong_book(self):
        data = {
            "books": {
                "read": [
                    {
                        "title": "Existing Match",
                        "author": "Author One",
                        "exclusive_shelf": "read",
                        "my_review": "kept",
                    },
                    {
                        "title": "Neighbor",
                        "author": "Author Two",
                        "exclusive_shelf": "read",
                    },
                ],
                "currently_reading": [],
                "to_read": [],
            }
        }

        rss_books_by_shelf = {
            "read": [
                {
                    "goodreads_id": "new-1",
                    "title": "Brand New",
                    "author": "Author Zero",
                    "exclusive_shelf": "read",
                },
                {
                    "goodreads_id": "existing-1",
                    "title": "Existing Match",
                    "author": "Author One",
                    "exclusive_shelf": "read",
                },
            ],
            "currently_reading": [],
            "to_read": [],
        }

        added, updated = _merge(copy.deepcopy(data), rss_books_by_shelf)

        self.assertEqual((added, updated), (1, 1))

        merged_read = copy.deepcopy(data)
        added, updated = _merge(merged_read, rss_books_by_shelf)
        self.assertEqual((added, updated), (1, 1))

        titles = [book["title"] for book in merged_read["books"]["read"]]
        self.assertEqual(titles.count("Existing Match"), 1)
        self.assertEqual(titles.count("Brand New"), 1)
        self.assertEqual(titles.count("Neighbor"), 1)

        existing_match = next(
            book for book in merged_read["books"]["read"] if book["title"] == "Existing Match"
        )
        self.assertEqual(existing_match["goodreads_id"], "existing-1")
        self.assertEqual(existing_match["my_review"], "kept")

    def test_dedupe_shelf_collapses_existing_duplicates_and_preserves_data(self):
        books = [
            {
                "title": "Duplicate",
                "author": "Author",
                "exclusive_shelf": "read",
                "date_read": "2024-01-10",
                "my_review": "kept",
            },
            {
                "goodreads_id": "gid-1",
                "title": "Duplicate",
                "author": "Author",
                "exclusive_shelf": "read",
                "date_read": "",
            },
            {
                "title": "Unique",
                "author": "Elsewhere",
                "exclusive_shelf": "read",
            },
        ]

        deduped = _dedupe_shelf(books)

        self.assertEqual(len(deduped), 2)
        duplicate = next(book for book in deduped if book["title"] == "Duplicate")
        self.assertEqual(duplicate["goodreads_id"], "gid-1")
        self.assertEqual(duplicate["my_review"], "kept")
        self.assertEqual(duplicate["date_read"], "2024-01-10")

    def test_sort_shelf_respects_field_priority_and_keeps_undated_last(self):
        books = [
            {"title": "No Date"},
            {"title": "Earlier", "date_read": "2024-01-10"},
            {"title": "Latest", "date_read": "2024-03-05"},
            {"title": "Fallback Added", "date_added": "2024-02-14"},
        ]

        sorted_books = _sort_shelf(books, "date_added", "date_read")

        self.assertEqual(
            [book["title"] for book in sorted_books],
            ["Fallback Added", "Latest", "Earlier", "No Date"],
        )


if __name__ == "__main__":
    unittest.main()
