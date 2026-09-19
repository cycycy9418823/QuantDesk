from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quantdesk.providers import news_provider


class TestSymbolCode(unittest.TestCase):
    def test_six_digit(self) -> None:
        self.assertEqual(news_provider._symbol_code("000001"), "000001")

    def test_with_prefix(self) -> None:
        self.assertEqual(news_provider._symbol_code("sz000001"), "000001")

    def test_empty(self) -> None:
        self.assertEqual(news_provider._symbol_code(""), "")
        self.assertEqual(news_provider._symbol_code(None), "")

    def test_short(self) -> None:
        self.assertEqual(news_provider._symbol_code("123"), "000123")


class TestFetchNewsOffline(unittest.TestCase):
    def test_invalid_symbol_returns_error(self) -> None:
        result = news_provider.fetch_news("", limit=5, network=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_symbol")

    def test_offline_no_cache_returns_no_news(self) -> None:
        with mock.patch.object(news_provider, "_read_local_news", return_value=[]):
            result = news_provider.fetch_news("999999", limit=5, network=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "no_news")
        self.assertEqual(result["data"], [])

    def test_offline_with_cache_returns_cached(self) -> None:
        cached = [{"title": "Test announcement", "content": "Test", "published": "2026-01-01", "source": "test", "url": "http://test"}]
        with mock.patch.object(news_provider, "_read_local_news", return_value=cached):
            result = news_provider.fetch_news("000001", limit=5, network=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "cached")
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["title"], "Test announcement")


class TestFetchNewsNetwork(unittest.TestCase):
    def test_network_fetch_success_caches(self) -> None:
        fake_items = [{"title": "Announcement A", "content": "Content A", "published": "2026-01-01", "source": "东方财富公告", "url": "http://a"}]
        with tempfile.TemporaryDirectory() as cache_dir, \
             mock.patch.object(news_provider, "_fetch_announce_eastmoney", return_value=fake_items), \
             mock.patch.object(news_provider, "NEWS_DIR", Path(cache_dir)):
            result = news_provider.fetch_news("000001", limit=10, network=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "fetched")
        self.assertEqual(len(result["data"]), 1)

    def test_network_fetch_failure_falls_back_to_cache(self) -> None:
        cached = [{"title": "Cached", "content": "C", "published": "2026-01-01", "source": "cache", "url": ""}]
        with mock.patch.object(news_provider, "_fetch_announce_eastmoney", side_effect=Exception("network error")), \
             mock.patch.object(news_provider, "_read_local_news", return_value=cached):
            result = news_provider.fetch_news("000001", limit=5, network=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "cached")
        self.assertIn("Live fetch failed", result["failed_reason"])

    def test_network_failure_no_cache_returns_no_news(self) -> None:
        with mock.patch.object(news_provider, "_fetch_announce_eastmoney", side_effect=Exception("network error")), \
             mock.patch.object(news_provider, "_read_local_news", return_value=[]):
            result = news_provider.fetch_news("000001", limit=5, network=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Live fetch failed", result["failed_reason"])
        self.assertIn("no cached news", result["failed_reason"])

    def test_insecure_ssl_request_is_ignored_and_visible(self) -> None:
        fake_items = [{"title": "Announcement A", "content": "Content A", "published": "2026-01-01", "source": "test", "url": "http://a"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(news_provider, "_fetch_announce_eastmoney", return_value=fake_items), \
                 mock.patch.object(news_provider, "NEWS_DIR", Path(temp_dir)):
                result = news_provider.fetch_news("000001", limit=10, network=True, insecure_ssl=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ssl_verification"], "default")
        self.assertIn("insecure_ssl_requested_but_ignored", result["warnings"])


class TestFetchNewsBatch(unittest.TestCase):
    def test_batch_multiple_symbols(self) -> None:
        with mock.patch.object(news_provider, "_read_local_news", return_value=[]):
            result = news_provider.fetch_news_batch(["000001", "000002"], limit_per_symbol=3, network=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols_checked"], 2)
        self.assertEqual(result["total_items"], 0)
        self.assertIn("000001", result["results"])
        self.assertIn("000002", result["results"])


class TestCapability(unittest.TestCase):
    def test_capability_offline(self) -> None:
        caps = news_provider.capability(network=False)
        self.assertEqual(len(caps), 2)
        self.assertEqual(caps[0]["provider"], "eastmoney_announce")
        self.assertEqual(caps[0]["status"], "offline")
        self.assertTrue(caps[0]["requires_network"])
        self.assertEqual(caps[1]["provider"], "local_news_cache")
        self.assertFalse(caps[1]["requires_network"])

    def test_capability_online(self) -> None:
        caps = news_provider.capability(network=True)
        self.assertEqual(caps[0]["status"], "ready")


class TestReadLocalNews(unittest.TestCase):
    def test_no_news_dir(self) -> None:
        with mock.patch.object(news_provider, "NEWS_DIR", Path("/nonexistent_path_12345")):
            items = news_provider._read_local_news("000001", 10)
        self.assertEqual(items, [])


class TestNewsCacheStats(unittest.TestCase):
    def test_no_dir(self) -> None:
        with mock.patch.object(news_provider, "NEWS_DIR", Path("/nonexistent_path_12345")):
            stats = news_provider.news_cache_stats()
        self.assertFalse(stats["exists"])
        self.assertEqual(stats["file_count"], 0)


if __name__ == "__main__":
    unittest.main()
