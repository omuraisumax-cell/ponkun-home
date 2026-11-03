"""Offline contract tests for the opt-in Mobile news feature."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import ponkun_news as news
from ponkun_openai_web import OpenAIWebError
from ponkun_news_ui import PAGE


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "PONKUN_NEWS_DB_PATH": str(Path(self.temp.name) / "news.sqlite3"),
            "PONKUN_OPENAI_WEB_ENABLED": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_topics_and_states(self):
        self.assertEqual(len(news.snapshot()["topics"]), 8)
        self.assertEqual(news.snapshot()["usage"]["daily_limit"], 20)
        self.assertEqual(news.snapshot()["usage"]["monthly_call_limit"], 300)
        self.assertEqual(news.snapshot()["usage"]["monthly_limit_usd"], 20.0)
        self.assertEqual(news.set_topics(["GPU", "税務制度"]), ["GPU", "税務制度"])
        self.assertEqual(news.snapshot()["topics"], ["GPU", "税務制度"])
        with self.assertRaises(news.NewsError):
            news.set_topics(["GPU", "gpu"])
        with self.assertRaises(news.NewsError):
            news.set_state("absent", "read")

    def test_limits_persist_separately_from_article_database(self):
        news.snapshot()
        with news._connect() as db:
            before = db.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
        self.assertEqual(news.set_limits(25, 320, 25.50), (25, 320, 25.5))
        self.assertEqual(news.snapshot()["usage"]["monthly_call_limit"], 320)
        self.assertEqual(news.snapshot()["usage"]["daily_limit"], 25)
        self.assertEqual(news.snapshot()["usage"]["monthly_limit_usd"], 25.5)
        self.assertTrue(news._limits_path().exists())
        with news._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM settings").fetchone()[0], before)
        for values in ((0, 300, 20), (20, 1001, 20), (20, 300, 0.01), (True, 300, 20)):
            with self.assertRaises(news.NewsError):
                news.set_limits(*values)
        self.assertEqual(news.snapshot()["usage"]["daily_limit"], 25)

    def test_jst_day_and_month_bounds(self):
        bounds = news._period_bounds(datetime(2026, 11, 2, 15, 5, tzinfo=timezone.utc))
        self.assertEqual(bounds[0], "2026-11-02T15:00:00+00:00")
        self.assertEqual(bounds[1], "2026-11-03T15:00:00+00:00")
        self.assertEqual(bounds[2], "2026-10-31T15:00:00+00:00")
        self.assertEqual(bounds[3], "2026-11-30T15:00:00+00:00")

    def test_refresh_deduplicates_cited_articles_and_persists_state(self):
        news.set_limits(2, 20, 1.0)
        url = "https://example.com/new-model?utm_source=test"
        answer = json.dumps([
            {"title": "新しいローカルLLMが公開された", "published_at": date.today().isoformat(),
             "url": url, "summary": "新しいモデルが公開されました。", "reason": "関心分野と一致します。", "topic": "ローカルLLM"},
            {"title": "新しいローカルLLMが公開された！", "published_at": date.today().isoformat(),
             "url": "https://example.com/duplicate", "summary": "重複。", "reason": "同じ内容。", "topic": "ローカルLLM"},
            {"title": "偽の記事が公開された", "published_at": date.today().isoformat(),
             "url": "https://not-cited.example/fake", "summary": "未確認。", "reason": "未確認。", "topic": "GPU"},
        ], ensure_ascii=False)
        meta = {"web_calls": 1, "input_tokens": 100, "output_tokens": 200}
        sources = [{"url": url}, {"url": "https://example.com/duplicate"}]
        with patch.object(news, "search_answer", return_value=(answer, sources, meta)) as search:
            first = news.refresh()
            second = news.refresh()
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(first["articles"]), 1)
        self.assertEqual(len(second["articles"]), 1)
        article = first["articles"][0]
        self.assertNotIn("utm_", article["url"])
        news.set_state(article["id"], "interested")
        self.assertEqual(news.snapshot()["articles"][0]["state"], "interested")
        self.assertEqual(news.snapshot()["usage"]["calls"], 2)
        with patch.object(news, "search_answer") as search:
            with self.assertRaises(news.NewsError) as raised:
                news.refresh()
            self.assertEqual(raised.exception.code, "budget_exceeded")
            search.assert_not_called()

    def test_disabled_search_never_calls_provider(self):
        with patch.dict(os.environ, {"PONKUN_OPENAI_WEB_ENABLED": "0"}):
            with patch.object(news, "search_answer") as search:
                with self.assertRaises(news.NewsError):
                    news.refresh()
                search.assert_not_called()

    def test_busy_refresh_never_calls_provider(self):
        self.assertTrue(news._refresh_lock.acquire(blocking=False))
        try:
            with patch.object(news, "search_answer") as search:
                with self.assertRaises(news.NewsError) as raised:
                    news.refresh()
                self.assertEqual(raised.exception.code, "refresh_busy")
                search.assert_not_called()
        finally:
            news._refresh_lock.release()

    def test_failed_search_is_not_retried(self):
        with patch.object(news, "search_answer", side_effect=OpenAIWebError("test_failure")) as search:
            with self.assertRaises(OpenAIWebError):
                news.refresh()
            search.assert_called_once()
        self.assertEqual(news.snapshot()["usage"]["calls"], 1)
        with news._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM usage").fetchone()[0], "failed")

    def test_monthly_call_and_estimated_cost_caps_block_provider(self):
        now = datetime.now(timezone.utc).isoformat()
        with news._connect() as db:
            db.executemany("INSERT INTO usage(created_at,status,estimated_usd) VALUES(?,?,?)",
                           [(now, "complete", 0.01)] * 300)
        with patch.object(news, "search_answer") as search:
            with self.assertRaises(news.NewsError) as raised:
                news.refresh()
            self.assertEqual(raised.exception.code, "budget_exceeded")
            search.assert_not_called()
        with news._connect() as db:
            db.execute("DELETE FROM usage")
            db.execute("INSERT INTO usage(created_at,status,estimated_usd) VALUES(?,?,?)",
                       (now, "complete", 19.95))
        with patch.object(news, "search_answer") as search:
            with self.assertRaises(news.NewsError) as raised:
                news.refresh()
            self.assertEqual(raised.exception.code, "budget_exceeded")
            search.assert_not_called()

    def test_invalid_saved_limits_fail_closed(self):
        news._limits_path().write_text('{"daily_limit": -1}', encoding="utf-8")
        with patch.object(news, "search_answer") as search:
            with self.assertRaises(news.NewsError) as raised:
                news.refresh()
            self.assertEqual(raised.exception.code, "invalid_limit")
            search.assert_not_called()

    def test_urls_do_not_store_credentials_or_local_addresses(self):
        self.assertIsNone(news._canonical_url("https://localhost/article"))
        self.assertIsNone(news._canonical_url("https://192.168.1.2/article"))
        self.assertEqual(news._canonical_url("https://example.com/story?id=42&token=private"),
                         "https://example.com/story?id=42")

    def test_mobile_routes_keep_existing_auth_boundary(self):
        source = Path(__file__).resolve().parents[1].joinpath("ponkun_mobile.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/news/api")', source)
        self.assertIn('@app.post("/news/api/refresh")', source)
        self.assertIn('@app.post("/news/api/limits")', source)
        self.assertIn('def news_limits(payload: NewsLimitsRequest, _auth: str | None = Depends(require_auth)):', source)
        self.assertIn('def news_refresh(_auth: str | None = Depends(require_auth)):', source)
        self.assertIn('def news_topics(payload: NewsTopicsRequest, _auth: str | None = Depends(require_auth)):', source)
        self.assertIn('def news_article_state(article_id: str, payload: NewsStateRequest, _auth: str | None = Depends(require_auth)):', source)

    def test_page_has_manual_refresh_safe_links_and_status_controls(self):
        self.assertIn('id="refresh"', PAGE)
        self.assertIn('id="saveLimits"', PAGE)
        self.assertIn("/news/api/limits", PAGE)
        self.assertIn("実際のAPI請求額", PAGE)
        self.assertIn("confirm(", PAGE)
        self.assertIn("textContent", PAGE)
        self.assertIn("noopener noreferrer", PAGE)
        self.assertIn("興味あり", PAGE)
        self.assertIn("興味なし", PAGE)
        self.assertIn("既読", PAGE)
        self.assertNotIn("setInterval(", PAGE)


if __name__ == "__main__":
    unittest.main()
