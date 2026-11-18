"""验证多平台持续翻页、空页复查、异常断点和原生分页信息保留。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from tools.collection_search import CollectionSearch
from tools.platform_adapters import Content, PlatformAdapter
from tools.search_paging import SearchPage, SearchResponseError, fetch_checked
from tools.weibo_requests import CollectionPaused


OPTIONS = dict(attempts=3, backoff=0, max_wait=60)
TEXT = "高速公路发生交通事故，多车追尾，交警正在现场处置。"


class PagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_empty_rechecks_same_page_before_using_result(self):
        fetch = AsyncMock(side_effect=[SearchPage([], {}, unconfirmed_empty=True), SearchPage([{"id": "1"}], {})])
        page = await fetch_checked(fetch, OPTIONS, "test")
        self.assertEqual(page.contexts, [{"id": "1"}])
        self.assertFalse(page.finished)
        self.assertEqual(fetch.await_count, 2)

    async def test_normal_empty_list_requires_confirmation(self):
        fetch = AsyncMock(side_effect=lambda: SearchPage([], {}, unconfirmed_empty=True))
        page = await fetch_checked(fetch, OPTIONS, "test")
        self.assertTrue(page.finished)
        self.assertEqual(fetch.await_count, 2)

    async def test_unknown_response_is_retried_then_paused(self):
        fetch = AsyncMock(side_effect=SearchResponseError("missing list"))
        with self.assertRaises(CollectionPaused):
            await fetch_checked(fetch, OPTIONS, "test")
        self.assertEqual(fetch.await_count, 3)

    async def test_verification_is_not_retried(self):
        fetch = AsyncMock(side_effect=CollectionPaused("verification"))
        with self.assertRaises(CollectionPaused):
            await fetch_checked(fetch, OPTIONS, "test")
        fetch.assert_awaited_once()

    async def test_all_six_platforms_continue_multiple_pages_without_quantity_cap(self):
        for platform in ("bili", "xhs", "dy", "ks", "tieba", "zhihu"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                settings = SimpleNamespace(
                    SAVE_DATA_PATH=tmp, SAVE_DATA_OPTION="sqlite", KEYWORDS="交通", START_PAGE=1,
                    CRAWLER_MAX_NOTES_COUNT=0, ENABLE_GET_COMMENTS=False,
                    SQLITE_EXPORT_EXCEL=False, SQLITE_EXPORT_JSONL=False,
                    COLLECTION_RETRY_BACKOFF_SEC=0)
                adapter = Mock()
                adapter.key = lambda row: row["id"]
                adapter.task_context = lambda row: row
                adapter.cached_content = lambda *args: None
                adapter.load = AsyncMock(side_effect=lambda row: Content(row["id"], row, TEXT))
                adapter.save = AsyncMock()
                adapter.fetch = AsyncMock(side_effect=[
                    SearchPage([{"id": "1"}], {"next": "two"}),
                    SearchPage([], {"next": "three"}, continue_empty=True),
                    SearchPage([{"id": "2"}], {"next": "four"}),
                    SearchPage([{"id": "3"}], {}, finished=True)])
                await CollectionSearch(platform, None, settings, adapter=adapter).run()
                self.assertEqual([call.args[1] for call in adapter.fetch.await_args_list], [1, 2, 3, 4])
                self.assertEqual(adapter.save.await_count, 3)

    async def test_failed_page_stays_unfinished_after_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(SAVE_DATA_PATH=tmp, SAVE_DATA_OPTION="sqlite", KEYWORDS="交通",
                START_PAGE=1, CRAWLER_MAX_NOTES_COUNT=0, COLLECTION_RETRY_BACKOFF_SEC=0)
            adapter = Mock()
            adapter.fetch = AsyncMock(side_effect=SearchResponseError("missing cursor"))
            with self.assertRaises(CollectionPaused):
                await CollectionSearch("ks", None, settings, adapter=adapter).run()
            with closing(sqlite3.connect(Path(tmp) / "ks/collection_state.db")) as conn:
                self.assertEqual(conn.execute('SELECT next_page,completed FROM search_progress').fetchone(), (1, 0))

    async def test_douyin_verify_check_does_not_finish_keyword_or_skip_to_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(SAVE_DATA_PATH=tmp, SAVE_DATA_OPTION="sqlite", KEYWORDS="交通,交通事故",
                START_PAGE=1, CRAWLER_MAX_NOTES_COUNT=0, PUBLISH_TIME_TYPE=0)
            client = Mock()
            client.search_info_by_keyword = AsyncMock(return_value={"status_code": 0, "data": [],
                "has_more": 0, "cursor": 0, "search_nil_info": {"search_nil_type": "verify_check"}})
            adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client), settings)
            with self.assertRaisesRegex(CollectionPaused, "verify_check"):
                await CollectionSearch("dy", None, settings, adapter=adapter).run()
            client.search_info_by_keyword.assert_awaited_once()
            with closing(sqlite3.connect(Path(tmp) / "dy/collection_state.db")) as conn:
                self.assertEqual(conn.execute('SELECT keyword,next_page,completed FROM search_progress ORDER BY rowid').fetchall(),
                                 [("交通", 1, 0), ("交通事故", 1, 0)])


class AdapterPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_douyin_most_liked_is_passed_to_search(self):
        from media_platform.douyin.field import SearchSortType

        client = Mock()
        client.search_info_by_keyword = AsyncMock(return_value={"data": [], "has_more": 0, "cursor": 0})
        adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client),
                                  SimpleNamespace(PUBLISH_TIME_TYPE=0, DY_SEARCH_SORT_TYPE=1))
        await adapter.fetch("交通", 1, {})
        self.assertIs(client.search_info_by_keyword.await_args.kwargs["sort_type"], SearchSortType.MOST_LIKE)

    async def test_douyin_native_search_sends_most_liked_filter(self):
        import json
        from media_platform.douyin.client import DouYinClient
        from media_platform.douyin.field import SearchSortType

        client = DouYinClient(headers={}, cookie_dict={}, playwright_page=Mock())
        client.get = AsyncMock(return_value={})
        await client.search_info_by_keyword("交通", sort_type=SearchSortType.MOST_LIKE)
        params = client.get.await_args.args[1]
        self.assertEqual(json.loads(params["filter_selected"]), {"sort_type": "1", "publish_time": "0"})
        self.assertEqual(params["is_filter_search"], 1)

    async def test_noncontent_pages_keep_platform_paging_information(self):
        cases = {
            "bili": ("bili_client", "search_video_by_keyword", {"result": [], "numPages": 3}),
            "xhs": ("xhs_client", "get_note_by_keyword", {"items": [{"model_type": "hot_query"}], "has_more": "1"}),
            "ks": ("ks_client", "search_info_by_keyword_v2", {"feeds": [{}], "pcursor": "next"}),
            "tieba": ("tieba_client", "get_notes_by_keyword", {"contents": [], "raw_count": 2, "has_more": True}),
            "zhihu": ("zhihu_client", "get_note_by_keyword", {
                "contents": [], "raw_count": 1, "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/search_v3?offset=60"}}),
        }
        for platform, (attribute, method, response) in cases.items():
            with self.subTest(platform=platform):
                client = Mock()
                setattr(client, method, AsyncMock(return_value=response))
                adapter = PlatformAdapter(platform, SimpleNamespace(**{attribute: client}), SimpleNamespace(SORT_TYPE=""))
                result = await adapter.fetch("交通", 1, {})
                self.assertEqual(result.contexts, [])
                self.assertFalse(result.finished)
                self.assertTrue(result.continue_empty)
                if platform == "zhihu":
                    self.assertEqual(result.cursor, {"offset": 60})

    async def test_missing_fields_are_not_treated_as_end(self):
        for platform, attribute, method in (
            ("bili", "bili_client", "search_video_by_keyword"),
            ("xhs", "xhs_client", "get_note_by_keyword"),
            ("ks", "ks_client", "search_info_by_keyword_v2")):
            with self.subTest(platform=platform):
                client = Mock()
                setattr(client, method, AsyncMock(return_value={}))
                adapter = PlatformAdapter(platform, SimpleNamespace(**{attribute: client}), SimpleNamespace(SORT_TYPE=""))
                with self.assertRaises(SearchResponseError):
                    await adapter.fetch("交通", 1, {})

    async def test_xhs_preserves_session_and_recognizes_string_end_flag(self):
        client = Mock()
        client.get_note_by_keyword = AsyncMock(return_value={"items": [], "has_more": "0"})
        adapter = PlatformAdapter("xhs", SimpleNamespace(xhs_client=client), SimpleNamespace(SORT_TYPE=""))
        page = await adapter.fetch("交通", 2, {"search_id": "session", "page": 2})
        self.assertTrue(page.finished)
        self.assertEqual(client.get_note_by_keyword.await_args.kwargs["search_id"], "session")

    async def test_tieba_keeps_entire_requested_page(self):
        from media_platform.tieba.client import BaiduTieBaClient

        client = BaiduTieBaClient(playwright_page=Mock())
        client._fetch_json_by_browser = AsyncMock(return_value={"no": 0, "data": {
            "has_more": 1, "card_list": [{"cardInfo": "thread", "data": {"tid": str(i), "title": TEXT}}
                                         for i in range(1, 4)]}})
        result = await client.get_notes_by_keyword("交通", page_size=1, with_paging=True)
        self.assertEqual([row.note_id for row in result["contents"]], ["1", "2", "3"])
        self.assertEqual(result["raw_count"], 3)

    async def test_zhihu_preserves_paging_even_without_supported_content(self):
        from media_platform.zhihu.client import ZhiHuClient

        client = ZhiHuClient(headers={}, cookie_dict={}, playwright_page=Mock())
        client.get = AsyncMock(return_value={"data": [{"type": "search_result", "object": {"type": "question"}}],
                                            "paging": {"is_end": False, "next": "https://www.zhihu.com/?offset=60"}})
        result = await client.get_note_by_keyword("交通", offset=40, with_paging=True)
        self.assertEqual(result["contents"], [])
        self.assertEqual(result["raw_count"], 1)
        self.assertFalse(result["paging"]["is_end"])
        self.assertEqual(client.get.await_args.args[1]["offset"], 40)


if __name__ == "__main__":
    unittest.main()
