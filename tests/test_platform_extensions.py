"""离线验证六个平台的传输、原生接口适配、续采和筛选隔离。"""

from contextlib import closing
import importlib
import inspect
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
from openpyxl import load_workbook
from sqlalchemy import create_engine

from database.models import Base
from model.m_baidu_tieba import TiebaNote
from model.m_zhihu import ZhihuContent
from tools.collection_search import CollectionSearch
from tools.platform_adapters import Content, PlatformAdapter, SearchPage
from tools.platform_http import BrowserTransport, PlatformHttpMixin
from tools.platform_specs import PLATFORMS
from tools.traffic_filter import filter_database
from tools.weibo_requests import CollectionPaused, WeiboRequests


TEXT = "高速公路发生交通事故，多车追尾，交警正在现场处置。"
CLIENTS = {"bili": ("bilibili", "BilibiliClient"), "xhs": ("xhs", "XiaoHongShuClient"),
           "dy": ("douyin", "DouYinClient"), "ks": ("kuaishou", "KuaiShouClient"),
           "tieba": ("tieba", "BaiduTieBaClient"), "zhihu": ("zhihu", "ZhiHuClient")}


def settings(root):
    return SimpleNamespace(SAVE_DATA_PATH=str(root), SQLITE_DB_PATH=str(root / "raw.db"),
        SAVE_DATA_OPTION="sqlite", KEYWORDS="交通事故", START_PAGE=1, CRAWLER_MAX_NOTES_COUNT=0,
        CRAWLER_MAX_SLEEP_SEC=0.5, ENABLE_GET_COMMENTS=True, ENABLE_GET_SUB_COMMENTS=False,
        CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=20, ENABLE_GET_MEDIA=False,
        SQLITE_EXPORT_EXCEL=True, SQLITE_EXPORT_JSONL=False, SORT_TYPE="", PUBLISH_TIME_TYPE=0,
        TRAFFIC_MIN_CONTENT_LEN=15, TRAFFIC_MIN_EVENT_DIMENSIONS=2,
        COLLECTION_SNAPSHOT_EVERY_POSTS=0, COLLECTION_SNAPSHOT_EVERY_SECONDS=0)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_unavailable_videos_do_not_pause_collection(self):
        from media_platform.bilibili.exception import DataFetchError

        for message in ("啥都木有", "稿件不可见"):
            client, _ = self.make_client("bili", {"code": -404, "message": message})
            for _ in range(4):
                with self.assertRaises(DataFetchError):
                    await client.request("GET", "https://api.bilibili.com/x/web-interface/view/detail?aid=1")
            self.assertEqual(getattr(client, "_collection_api_errors", 0), 0)

    async def test_bilibili_other_errors_still_pause(self):
        from media_platform.bilibili.exception import DataFetchError

        client, _ = self.make_client("bili", {"code": -400, "message": "请求错误"})
        for _ in range(2):
            with self.assertRaises(DataFetchError):
                await client.request("GET", "https://api.bilibili.com/x/web-interface/view/detail?aid=1")
        with self.assertRaises(CollectionPaused):
            await client.request("GET", "https://api.bilibili.com/x/web-interface/view/detail?aid=1")

    def make_client(self, platform, payload, status=200):
        module, name = CLIENTS[platform]
        cls = getattr(importlib.import_module(f"media_platform.{module}.client"), name)
        offered = dict(headers={}, playwright_page=Mock(), cookie_dict={}, proxy=None)
        arguments = {key: value for key, value in offered.items() if key in inspect.signature(cls).parameters}
        client = cls(**arguments)
        factory = Mock(side_effect=lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload))))
        client._collection_pool = WeiboRequests(factory, interval=0, backoff=0,
            paused_type=CollectionPaused, login_hosts=())
        self.addAsyncCleanup(client.aclose)
        return client, factory

    async def test_all_native_clients_reuse_pool(self):
        replies = {"bili": {"code": 0, "data": {}}, "xhs": {"success": True, "code": 0, "data": {}},
                   "dy": {"status_code": 0, "data": []}, "ks": {"data": {}}, "tieba": {"error_code": 0}, "zhihu": {"data": []}}
        for platform, payload in replies.items():
            with self.subTest(platform=platform):
                client, factory = self.make_client(platform, payload)
                self.assertIsInstance(client, PlatformHttpMixin)
                await client.request("GET", "https://example.test/api")
                await client.request("GET", "https://example.test/api")
                self.assertEqual(factory.call_count, 1)

    async def test_all_clients_pause_on_verification(self):
        for platform in CLIENTS:
            with self.subTest(platform=platform):
                client, _ = self.make_client(platform, {}, status=403)
                with self.assertRaises(CollectionPaused):
                    await client.request("GET", "https://example.test/api")
                self.assertEqual(client.request_pool.stats["requests"], 1)

    async def test_douyin_verify_check_with_success_code_pauses_immediately(self):
        client, _ = self.make_client("dy", {"status_code": 0, "data": [], "has_more": 0, "cursor": 0,
            "search_nil_info": {"search_nil_type": "verify_check", "search_nil_item": "verify_check"}})
        with self.assertRaisesRegex(CollectionPaused, "verify_check"):
            await client.request("GET", "https://www.douyin.com/aweme/v1/web/general/search/single/")
        self.assertEqual(client.request_pool.stats["requests"], 1)

    async def test_zhihu_empty_comment_404(self):
        client, _ = self.make_client("zhihu", {}, status=404)
        self.assertEqual(await client.request("GET", "https://www.zhihu.com/api/v4/answers/1/root_comments"), {})

    async def test_zhihu_last_comment_page_is_saved_with_limit(self):
        client, _ = self.make_client("zhihu", {})
        client.get_root_comments = AsyncMock(return_value={"data": [{}], "paging": {"is_end": True}})
        client._extractor = Mock()
        client._extractor.extract_offset.return_value = ""
        client._extractor.extract_comments.return_value = list(range(30))
        client.get_comments_all_sub_comments = AsyncMock()
        callback = AsyncMock()
        comments = await client.get_note_all_comments(ZhihuContent(content_id="1", content_type="answer"),
                                                      crawl_interval=0, callback=callback, max_count=20)
        self.assertEqual(comments, list(range(20)))
        callback.assert_awaited_once_with(list(range(20)))
        client.get_root_comments.assert_awaited_once()

    async def test_tieba_browser_transport_uses_same_retry_control(self):
        client, _ = self.make_client("tieba", {})
        await client.aclose()
        client.playwright_page = Mock(url="https://tieba.baidu.com")
        client.playwright_page.evaluate = AsyncMock(side_effect=[
            {"status": 429, "text": "", "retryAfter": "0"},
            {"status": 200, "text": '{"error_code":0,"data":{}}'},
        ])
        client._collection_pool = WeiboRequests(lambda proxy: httpx.AsyncClient(transport=BrowserTransport(client)),
            interval=0, backoff=0, paused_type=CollectionPaused, login_hosts=())
        result = await client._fetch_json_by_browser("/mo/q/test", params={})
        self.assertEqual(result["error_code"], 0)
        self.assertEqual(client.playwright_page.evaluate.await_count, 2)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_search_uses_play_count_order(self):
        from media_platform.bilibili.field import SearchOrderType

        client = Mock()
        client.search_video_by_keyword = AsyncMock(return_value={"result": [], "numPages": 0})
        options = settings(Path('.'))
        options.BILI_SEARCH_ORDER = "click"
        adapter = PlatformAdapter("bili", SimpleNamespace(bili_client=client), options)
        await adapter.fetch("交通", 1, {})
        self.assertIs(client.search_video_by_keyword.await_args.kwargs["order"], SearchOrderType.MOST_CLICK)

    async def test_douyin_empty_page_with_more_results_keeps_paging(self):
        client = Mock()
        client.search_info_by_keyword = AsyncMock(return_value={
            "status_code": 0, "data": [{"type": "related_search"}], "has_more": 1, "cursor": 30})
        adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client), settings(Path('.')))
        page = await adapter.fetch("交通", 2, {"offset": 15, "search_id": "session"})
        self.assertEqual(page.contexts, [])
        self.assertFalse(page.finished)
        self.assertTrue(page.continue_empty)
        self.assertEqual(page.cursor, {"offset": 30, "search_id": "session"})

    async def test_douyin_invalid_or_ambiguous_response_pauses(self):
        responses = [{}, {"status_code": 4, "data": []}, {"data": []},
                     {"data": None, "has_more": 1, "cursor": 0},
                     {"data": [], "has_more": 1, "cursor": "invalid"}]
        for response in responses:
            with self.subTest(response=response):
                client = Mock()
                client.search_info_by_keyword = AsyncMock(return_value=response)
                adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client), settings(Path('.')))
                with self.assertRaises(CollectionPaused):
                    await adapter.fetch("交通", 1, {})

    async def test_douyin_explicit_last_page_keeps_its_video(self):
        client = Mock()
        client.search_info_by_keyword = AsyncMock(return_value={
            "status_code": "0", "data": [{"aweme_info": {"aweme_id": "1", "desc": TEXT}}],
            "has_more": "0", "cursor": 15})
        adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client), settings(Path('.')))
        page = await adapter.fetch("交通", 1, {})
        self.assertTrue(page.finished)
        self.assertEqual(page.contexts[0]["id"], "1")

    async def test_native_search_detail_comment_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tieba = TiebaNote(note_id="1", title=TEXT, note_url="https://tieba.baidu.com/p/1", tieba_name="交通", tieba_link="https://tieba.baidu.com/f", total_replay_page=1)
            zhihu = ZhihuContent(content_id="1", content_type="answer", content_text=TEXT)
            search_results = {
                "bili": {"result": [{"aid": 1}], "numPages": 1},
                "xhs": {"items": [{"id": "1", "xsec_token": "opaque-token"}], "has_more": False},
                "dy": {"data": [{"aweme_info": {"aweme_id": "1", "desc": TEXT}}], "has_more": 0, "cursor": 15},
                "ks": {"feeds": [{"photo": {"id": "1", "caption": TEXT}}], "pcursor": "no_more"},
                "tieba": {"contents": [tieba], "raw_count": 1, "has_more": False},
                "zhihu": {"contents": [zhihu], "raw_count": 1, "paging": {"is_end": True}},
            }
            for platform in CLIENTS:
                with self.subTest(platform=platform):
                    client = Mock()
                    client.search_video_by_keyword = AsyncMock(return_value=search_results[platform])
                    client.get_note_by_keyword = AsyncMock(return_value=search_results[platform])
                    client.search_info_by_keyword = AsyncMock(return_value=search_results[platform])
                    client.search_info_by_keyword_v2 = AsyncMock(return_value=search_results[platform])
                    client.get_notes_by_keyword = AsyncMock(return_value=search_results[platform])
                    client.get_video_info = AsyncMock(return_value={"View": {"aid": 1, "title": TEXT}})
                    client.get_note_by_id = AsyncMock(return_value=tieba if platform == "tieba" else {"note_id": "1", "title": TEXT})
                    client.get_video_all_comments = AsyncMock(return_value=[])
                    client.get_note_all_comments = AsyncMock(return_value=[])
                    client.get_aweme_all_comments = AsyncMock(return_value=[])
                    crawler = SimpleNamespace(**{PLATFORMS[platform].client: client})
                    adapter = PlatformAdapter(platform, crawler, settings(root))
                    adapter.store = Mock()
                    for updater in ["update_bilibili_video", "update_xhs_note", "update_douyin_aweme", "update_kuaishou_video", "update_tieba_note", "update_zhihu_content"]:
                        setattr(adapter.store, updater, AsyncMock())
                    page = await adapter.fetch("交通事故", 1, {})
                    self.assertEqual(len(page.contexts), 1)
                    content = await adapter.load(page.contexts[0])
                    await adapter.save(content)
                    await adapter.comments(content)
                    self.assertIn("交通事故", content.text)
                    self.assertNotIn("data", content.context)
                    if platform == "xhs":
                        self.assertTrue(page.finished)
                        self.assertEqual(client.get_note_all_comments.await_args.kwargs["xsec_token"], "opaque-token")
                    if platform in ("dy", "ks"):
                        client.get_video_info.assert_not_awaited()
                    if platform == "zhihu":
                        self.assertEqual(content.key, "1:answer")
                    call = (client.get_aweme_all_comments if platform == "dy" else client.get_video_all_comments if platform in ("bili", "ks") else client.get_note_all_comments).await_args
                    self.assertEqual(call.kwargs["max_count"], 20)

    async def test_zhihu_content_types_have_distinct_keys(self):
        adapter = object.__new__(PlatformAdapter)
        adapter.platform = "zhihu"
        self.assertNotEqual(adapter.key({"model": {"content_id": "1", "content_type": "answer"}}),
                            adapter.key({"model": {"content_id": "1", "content_type": "article"}}))


class FilterTests(unittest.TestCase):
    def test_seven_platforms_export_separate_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db = Path(tmp), Path(tmp) / "raw.db"
            engine = create_engine(f"sqlite:///{db}")
            Base.metadata.create_all(engine)
            engine.dispose()
            with closing(sqlite3.connect(db)) as conn:
                for platform, spec in PLATFORMS.items():
                    row = {spec.keys[0]: "1", "last_modify_ts": 1700086400000, spec.published: 1700000000}
                    if platform == "bili":
                        row["video_url"] = "https://www.bilibili.com/video/av1"
                    if platform == "zhihu":
                        row["content_type"] = "answer"
                    row.update({field: TEXT for field in spec.texts})
                    row.update({field: "10" for field in spec.metrics})
                    columns = ",".join(f'"{key}"' for key in row)
                    placeholders = ",".join("?" for _ in row)
                    conn.execute(f'INSERT INTO "{spec.posts}" ({columns}) VALUES ({placeholders})', list(row.values()))
                    comment = {spec.keys[0]: "1", "last_modify_ts": 1700086400000, "comment_id": "c1", "content": "注意安全"}
                    if platform == "zhihu":
                        comment["content_type"] = "answer"
                    columns = ",".join(f'"{key}"' for key in comment)
                    placeholders = ",".join("?" for _ in comment)
                    conn.execute(f'INSERT INTO "{spec.comments}" ({columns}) VALUES ({placeholders})', list(comment.values()))
                conn.commit()
            before = db.read_bytes()
            for platform, spec in PLATFORMS.items():
                with self.subTest(platform=platform):
                    output, summary = filter_database(db, root / "out", platform=platform)
                    self.assertEqual(output.parent.parent.parent.name, platform)
                    self.assertEqual((summary["交通相关帖子数"], summary["热点帖子数"], summary["相关评论数"]), (1, 1, 1))
                    self.assertEqual(summary["热度模型"], spec.heat_method)
                    with closing(sqlite3.connect(output)) as conn:
                        self.assertEqual(conn.execute('SELECT count(*) FROM traffic_hot_comment').fetchone()[0], 1)
                    workbook = load_workbook(next(output.parent.glob("*.xlsx")))
                    self.assertIn("人工抽检", workbook.sheetnames)
                    workbook.close()
            self.assertEqual(db.read_bytes(), before)


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, pages):
        item = Mock()
        item.key = lambda context: context["id"]
        item.task_context = lambda context: context
        item.cached_content = lambda *args: None
        item.fetch = AsyncMock(side_effect=pages)
        item.load = AsyncMock(side_effect=lambda context: Content(context["id"], context, TEXT))
        item.save = AsyncMock()
        item.comments = AsyncMock()
        return item

    async def test_changed_douyin_sort_does_not_reuse_other_sort_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = settings(Path(tmp))
            options.DY_SEARCH_SORT_TYPE = 0
            first = self.adapter([SearchPage([{"id": "1"}], {"offset": 15}), RuntimeError("network")])
            with self.assertRaises(RuntimeError):
                await CollectionSearch("dy", None, options, adapter=first).run()
            options.DY_SEARCH_SORT_TYPE = 1
            second = self.adapter([SearchPage([], {}, finished=True)])
            await CollectionSearch("dy", None, options, adapter=second).run()
            self.assertEqual(second.fetch.await_args.args, ("交通事故", 1, {}))
            with closing(sqlite3.connect(Path(tmp) / "dy/collection_state.db")) as conn:
                self.assertEqual(conn.execute('SELECT count(DISTINCT scope) FROM search_progress').fetchone()[0], 2)

    async def test_search_continues_forward_through_empty_page_then_next_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = settings(Path(tmp))
            options.KEYWORDS = "交通,车祸"
            first = {"offset": 15, "search_id": "session"}
            second = {"offset": 30, "search_id": "session"}
            adapter = self.adapter([
                SearchPage([{"id": "1"}], first, continue_empty=True),
                SearchPage([], second, continue_empty=True),
                SearchPage([{"id": "2"}], {"offset": 45}, finished=True),
                SearchPage([], {}, finished=True),
            ])
            await CollectionSearch("dy", None, options, adapter=adapter).run()
            self.assertEqual([call.args for call in adapter.fetch.await_args_list], [
                ("交通", 1, {}), ("交通", 2, first), ("交通", 3, second), ("车祸", 1, {})])
            self.assertEqual(adapter.save.await_count, 2)
            self.assertEqual(adapter.comments.await_count, 2)

    async def test_ambiguous_search_preserves_page_for_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = settings(Path(tmp))
            client = Mock()
            client.search_info_by_keyword = AsyncMock(return_value={})
            adapter = PlatformAdapter("dy", SimpleNamespace(dy_client=client), options)
            with self.assertRaises(CollectionPaused):
                await CollectionSearch("dy", None, options, adapter=adapter).run()
            with closing(sqlite3.connect(Path(tmp) / "dy" / "collection_state.db")) as conn:
                self.assertEqual(conn.execute('SELECT next_page,completed FROM search_progress').fetchone(), (1, 0))
            adapter = self.adapter([SearchPage([], {}, finished=True)])
            await CollectionSearch("dy", None, options, adapter=adapter).run()
            self.assertEqual(adapter.fetch.await_args.args, ("交通事故", 1, {}))

    async def test_repeated_video_page_does_not_restart_at_first_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = settings(Path(tmp))
            adapter = self.adapter([SearchPage([{"id": "1"}], {"offset": offset}, continue_empty=True) for offset in (15, 30)])
            await CollectionSearch("dy", None, options, adapter=adapter).run()
            self.assertEqual(adapter.fetch.await_count, 2)
            self.assertEqual(adapter.save.await_count, 1)
            with closing(sqlite3.connect(Path(tmp) / "dy" / "collection_state.db")) as conn:
                self.assertEqual(conn.execute('SELECT next_page,completed FROM search_progress').fetchone(), (3, 1))

    async def test_each_platform_resumes_cursor_and_keeps_state_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for platform in CLIENTS:
                with self.subTest(platform=platform):
                    def adapter(pages):
                        item = Mock()
                        item.key = lambda context: context["id"]
                        item.task_context = lambda context: context
                        item.cached_content = lambda *args: None
                        item.fetch = AsyncMock(side_effect=pages)
                        item.load = AsyncMock(side_effect=lambda context: Content(context["id"], context, TEXT))
                        item.save = AsyncMock()
                        item.comments = AsyncMock()
                        return item
                    first_adapter = adapter([SearchPage([{"id": "1"}], {"pcursor": "next"}), RuntimeError("network")])
                    with self.assertRaises(RuntimeError):
                        await CollectionSearch(platform, None, settings(root), adapter=first_adapter).run()
                    second_adapter = adapter([SearchPage([], {}, True)])
                    await CollectionSearch(platform, None, settings(root), adapter=second_adapter).run()
                    args = second_adapter.fetch.await_args.args
                    self.assertEqual((args[1], args[2]), (2, {"pcursor": "next"}))
                    self.assertTrue((root / platform / "collection_state.db").exists())


if __name__ == "__main__":
    unittest.main()
