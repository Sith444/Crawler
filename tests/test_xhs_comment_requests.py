"""验证小红书评论请求来源、浏览器信息及拒绝响应的暂停处理。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import config
from playwright.async_api import TimeoutError as BrowserTimeoutError

from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.core import XiaoHongShuCrawler
from tools.weibo_requests import CollectionPaused, WeiboRequests


class XhsCommentRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mode = patch.object(config, "XHS_COMMENT_MODE", "api")
        mode.start()
        self.addCleanup(mode.stop)

    def make_client(self, handler):
        client = XiaoHongShuClient(
            headers={"Cookie": "web_session=private-cookie", "referer": "https://www.xiaohongshu.com/"},
            playwright_page=Mock(), cookie_dict={},
        )
        client._refresh_proxy_if_expired = AsyncMock()
        client._pre_headers = AsyncMock(return_value=client.headers)
        client._collection_pool = WeiboRequests(
            lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            interval=0, attempts=1, label="xhs", paused_type=CollectionPaused,
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_comment_source_and_encoded_token(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"success": True, "data": {"comments": [{"id": "c1"}]}})

        client = self.make_client(respond)
        result = await client.get_note_comments("note1", "token+/=")
        self.assertEqual(result["comments"], [{"id": "c1"}])
        request = requests[0]
        self.assertEqual(request.url.params["xsec_token"], "token+/=")
        referer = urlparse(request.headers["referer"])
        self.assertEqual(referer.path, "/explore/note1")
        self.assertEqual(parse_qs(referer.query)["xsec_token"], ["token+/="])
        self.assertEqual(client.headers["referer"], "https://www.xiaohongshu.com/")

    async def test_406_pauses_once_without_logging_credentials(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(406, json={"code": -1, "success": False})

        client = self.make_client(respond)
        with self.assertLogs("MediaCrawler", level="WARNING") as logs:
            with self.assertRaises(CollectionPaused):
                await client.get_note_comments("note1", "private-token")
        self.assertEqual(len(requests), 1)
        output = "\n".join(logs.output)
        self.assertIn("code=-1", output)
        self.assertIn("/api/sns/web/v2/comment/page", output)
        self.assertNotIn("private-token", output)
        self.assertNotIn("private-cookie", output)

    async def test_browser_user_agent_is_used(self):
        crawler = XiaoHongShuCrawler()
        crawler.browser_context = Mock()
        crawler.context_page = Mock()
        crawler.context_page.evaluate = AsyncMock(return_value="actual-browser-user-agent")
        with patch("media_platform.xhs.core.utils.convert_browser_context_cookies",
                   new=AsyncMock(return_value=("web_session=test", {}))):
            client = await crawler.create_xhs_client(None)
        self.addAsyncCleanup(client.aclose)
        self.assertEqual(client.headers["user-agent"], "actual-browser-user-agent")
        self.assertNotIn("sec-ch-ua-platform", client.headers)

    async def test_browser_comment_page_and_scroll_pagination(self):
        client = self.make_client(lambda request: self.fail("不应发出独立HTTP请求"))
        page = Mock()
        page.goto = AsyncMock()
        scroller = Mock()
        scroller.evaluate = AsyncMock()
        page.locator.return_value = scroller
        client.playwright_page = page
        for cursor in ("", "next"):
            response = Mock(status=200)
            response.json = AsyncMock(return_value={"success": True, "data": {"comments": [{"id": "c1"}], "has_more": False}})
            pending = Mock()
            pending.value = asyncio.get_running_loop().create_future()
            pending.value.set_result(response)
            manager = Mock()
            manager.__aenter__ = AsyncMock(return_value=pending)
            manager.__aexit__ = AsyncMock(return_value=False)
            page.expect_response.return_value = manager
            with patch.object(config, "XHS_COMMENT_MODE", "browser"):
                result = await client.get_note_comments("note1", "token", cursor)
            self.assertEqual(result["comments"], [{"id": "c1"}])
            predicate = page.expect_response.call_args.args[0]
            self.assertTrue(predicate(Mock(url=f"https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id=note1&cursor={cursor}")))
            self.assertFalse(predicate(Mock(url="https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id=other")))
        page.goto.assert_awaited_once()
        scroller.evaluate.assert_awaited_once()

    async def test_browser_timeout_pauses_without_api_fallback(self):
        client = self.make_client(lambda request: self.fail("不应在网页失败后重试接口"))
        client.playwright_page = Mock()
        manager = Mock()
        manager.__aenter__ = AsyncMock(side_effect=BrowserTimeoutError("timeout"))
        manager.__aexit__ = AsyncMock(return_value=False)
        client.playwright_page.expect_response.return_value = manager
        with patch.object(config, "XHS_COMMENT_MODE", "browser"):
            with self.assertRaises(CollectionPaused):
                await client.get_note_comments("note1", "token")


if __name__ == "__main__":
    unittest.main()
