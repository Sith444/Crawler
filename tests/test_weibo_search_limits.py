"""离线验证微博搜索数量、去重、断点和周期导出。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tools.weibo_search import WeiboSearch, needs_comments
from tools.weibo_state import WeiboState
from tools.weibo_requests import WeiboPaused


def post(note_id, text="高速发生交通事故，货车追尾，交警正在现场处置。", long=False, likes=10):
    return {"mblog": {"id": note_id, "text": text, "isLongText": long, "attitudes_count": likes}}


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(
            SAVE_DATA_PATH=str(self.root), SAVE_DATA_OPTION="sqlite", START_PAGE=1,
            CRAWLER_MAX_NOTES_COUNT=0, WEIBO_SEARCH_TYPE="default", KEYWORDS="交通事故",
            COLLECTION_RESUME_SEARCH=True, ENABLE_WEIBO_FULL_TEXT=True,
            COLLECTION_DETAIL_REFRESH_HOURS=24, COLLECTION_COMMENT_REFRESH_HOURS=24,
            ENABLE_GET_COMMENTS=True, CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=20,
            COLLECTION_STAGED_COMMENTS=True, TRAFFIC_MIN_CONTENT_LEN=15, TRAFFIC_MIN_EVENT_DIMENSIONS=2,
            SQLITE_EXPORT_EXCEL=True, SQLITE_EXPORT_JSONL=False,
            COLLECTION_SNAPSHOT_EVERY_POSTS=0, COLLECTION_SNAPSHOT_EVERY_SECONDS=0,
        )

    def build(self, pages, *, confirmed_empty=True):
        if confirmed_empty:
            pages = [dict(page, _empty_confirmed=True) if isinstance(page, dict) and page.get("cards") == [] else page for page in pages]
        saved = []
        async def save(item):
            saved.append(deepcopy(item))
        store = SimpleNamespace(update_weibo_note=AsyncMock(side_effect=save), batch_update_weibo_note_comments=AsyncMock())
        crawler = SimpleNamespace(wb_client=SimpleNamespace(
            get_note_by_keyword=AsyncMock(side_effect=pages),
            get_note_info_by_id=AsyncMock(return_value=post("a", text="高速发生交通事故，货车追尾，交警处置，这是完整正文。")),
            get_note_all_comments=AsyncMock(return_value=[])), download_media=AsyncMock())
        search = WeiboSearch(crawler, self.config, store, Mock(), lambda cards: cards, SimpleNamespace(DEFAULT=0))
        return search, crawler, saved

    async def test_unlimited_and_repeated_page_stop(self):
        search, crawler, saved = self.build([{"cards": [post("a")]}, {"cards": [post("b")]}, {"cards": [post("a"), post("b")]}])
        await search.run()
        self.assertEqual(len(saved), 2)
        self.assertEqual(crawler.wb_client.get_note_by_keyword.await_count, 3)

    async def test_nonpost_card_and_transient_empty_do_not_end_search_early(self):
        self.config.COLLECTION_RETRY_BACKOFF_SEC = 0
        search, crawler, saved = self.build([
            {"cards": [{"card_type": 7}]}, {"cards": []}, {"cards": [post("a")]},
            {"cards": []}, {"cards": []}], confirmed_empty=False)
        search.card_filter = lambda cards: [item for item in cards if item.get("mblog")]
        await search.run()
        self.assertEqual(len(saved), 1)
        self.assertEqual([call.kwargs["page"] for call in crawler.wb_client.get_note_by_keyword.await_args_list], [1, 2, 2, 3, 3])

    async def test_finite_limit(self):
        self.config.CRAWLER_MAX_NOTES_COUNT = 1
        search, _, saved = self.build([{"cards": [post("a"), post("b")]}])
        await search.run()
        self.assertEqual(len(saved), 1)

    async def test_search_runs_before_old_failure_retries(self):
        search, crawler, saved = self.build([{"cards": [post("a")]}, {"cards": []}])
        async def retry():
            self.assertEqual(len(saved), 1)
            self.assertEqual(crawler.wb_client.get_note_by_keyword.await_count, 2)
        search.retry_failed = AsyncMock(side_effect=retry)
        await search.run()
        search.retry_failed.assert_awaited_once()

    async def test_cross_keyword_cache_preserves_fresh_interactions(self):
        self.config.KEYWORDS = "交通事故,追尾"
        search, crawler, saved = self.build([
            {"cards": [post("a", long=True)]}, {"cards": []},
            {"cards": [post("a", long=True, likes=99)]}, {"cards": []},
        ])
        await search.run()
        self.assertEqual(crawler.wb_client.get_note_info_by_id.await_count, 1)
        self.assertEqual(crawler.wb_client.get_note_all_comments.await_count, 1)
        self.assertEqual(saved[-1]["mblog"]["attitudes_count"], 99)
        self.assertIn("完整正文", saved[-1]["mblog"]["text"])
        self.assertGreater(saved[-1]["_observed_at"], 0)
        state = WeiboState(self.root / "wb/collection_state.db")
        try:
            self.assertEqual(state.conn.execute("SELECT count(*) FROM search_hits").fetchone()[0], 2)
        finally:
            state.close()

    async def test_resume_from_failed_page(self):
        first, _, _ = self.build([{"cards": [post("a")]}, RuntimeError("network")])
        with self.assertRaises(RuntimeError):
            await first.run()
        second, crawler, saved = self.build([{"cards": [post("b")]}, {"cards": []}])
        await second.run()
        self.assertEqual(crawler.wb_client.get_note_by_keyword.await_args_list[0].kwargs["page"], 2)
        self.assertEqual(saved[0]["mblog"]["id"], "b")

    async def test_failed_comments_are_recorded_and_retried(self):
        first, crawler, _ = self.build([{"cards": [post("a")]}, {"cards": []}])
        crawler.wb_client.get_note_all_comments.side_effect = RuntimeError("comment failure")
        await first.run()
        second, crawler, _ = self.build([{"cards": []}])
        await second.run()
        self.assertEqual(crawler.wb_client.get_note_info_by_id.await_count, 1)
        self.assertEqual(crawler.wb_client.get_note_all_comments.await_count, 1)
        state = WeiboState(self.root / "wb/collection_state.db")
        try:
            self.assertEqual(state.failed("comments"), [])
        finally:
            state.close()

    async def test_irrelevant_comments_deferred_but_post_saved(self):
        search, crawler, saved = self.build([{"cards": [post("a", "今天去看电影，演员的表演非常精彩，观众纷纷鼓掌。 ")]}, {"cards": []}])
        await search.run()
        self.assertEqual(len(saved), 1)
        crawler.wb_client.get_note_all_comments.assert_not_awaited()
        self.assertTrue(needs_comments("发生交通事故"))
        self.assertTrue(needs_comments("救命"))

    async def test_periodic_snapshot(self):
        self.config.COLLECTION_SNAPSHOT_EVERY_POSTS = 1
        search, _, _ = self.build([{"cards": [post("a")]}, {"cards": []}])
        with patch("tools.sqlite_exporter.export_from_config") as export:
            await search.run()
            export.assert_called_once()

    async def test_pause_during_comments_replays_unfinished_page(self):
        first, crawler, _ = self.build([{"cards": [post("a")]}])
        crawler.wb_client.get_note_all_comments.side_effect = WeiboPaused("verification")
        with self.assertRaises(WeiboPaused):
            await first.run()
        second, crawler, _ = self.build([{"cards": [post("a")]}, {"cards": []}])
        await second.run()
        self.assertEqual(crawler.wb_client.get_note_by_keyword.await_args_list[0].kwargs["page"], 1)
        self.assertEqual(crawler.wb_client.get_note_all_comments.await_count, 1)

    async def test_empty_detail_is_pending_not_cached_as_complete(self):
        search, crawler, _ = self.build([{"cards": [post("a", long=True)]}, {"cards": []}])
        crawler.wb_client.get_note_info_by_id.return_value = {"mblog": {"id": "a", "text": ""}}
        await search.run()
        state = WeiboState(self.root / "wb/collection_state.db")
        try:
            self.assertEqual(state.failed("detail"), ["a"])
            self.assertIsNone(state.cached_detail("a", 24))
        finally:
            state.close()

    def test_cache_expiry_and_larger_comment_limit(self):
        state = WeiboState(self.root / "wb/state_test.db")
        try:
            state.save_detail("a", "正文")
            state.save_comments("a", 20)
            self.assertEqual(state.cached_detail("a", 24), "正文")
            self.assertTrue(state.comments_fresh("a", 24, 20))
            self.assertFalse(state.comments_fresh("a", 24, 30))
            self.assertIsNone(state.cached_detail("a", 0))
            self.assertFalse(state.comments_fresh("a", 0, 20))
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
