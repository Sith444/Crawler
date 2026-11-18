"""离线验证连接复用、限流等待、暂停以及正常空结果。"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from media_platform.weibo.client import WeiboClient
from media_platform.weibo.exception import DataFetchError
from tools.weibo_requests import WeiboPaused, WeiboRequests, empty_result, retry_after_seconds


class RequestTests(unittest.IsolatedAsyncioTestCase):
    def pool(self, handler, **settings):
        factory = Mock(side_effect=lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        pool = WeiboRequests(factory, interval=0, backoff=0, **settings)
        self.addAsyncCleanup(pool.close)
        return pool, factory

    async def test_connection_pool_reused_and_closed(self):
        pool, factory = self.pool(lambda request: httpx.Response(200, json={"ok": 1}))
        await pool.send("GET", "https://m.weibo.cn/api/config")
        await pool.send("GET", "https://m.weibo.cn/api/config")
        factory.assert_called_once()
        client = pool.client
        await pool.close()
        self.assertTrue(client.is_closed)

    async def test_global_interval_covers_all_endpoints(self):
        pool, _ = self.pool(lambda request: httpx.Response(200))
        pool.interval, pool.clock, pool.sleep = 0.5, lambda: 0, AsyncMock()
        await pool.send("GET", "https://m.weibo.cn/api/config")
        await pool.send("GET", "https://m.weibo.cn/detail/1")
        self.assertEqual([call.args[0] for call in pool.sleep.await_args_list], [0, 0.5])

    async def test_429_waits_retry_after(self):
        replies = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
        pool, _ = self.pool(lambda request: next(replies))
        pool.sleep = AsyncMock()
        await pool.send("GET", "https://m.weibo.cn/api/config")
        self.assertIn(7, [call.args[0] for call in pool.sleep.await_args_list])
        self.assertEqual(pool.stats["retries"], 1)

    async def test_long_server_wait_pauses_without_early_retry(self):
        pool, _ = self.pool(lambda request: httpx.Response(429, headers={"Retry-After": "3600"}), max_wait=60)
        with self.assertRaises(WeiboPaused):
            await pool.send("GET", "https://m.weibo.cn/api/config")
        self.assertEqual(pool.stats["requests"], 1)

    async def test_verification_status_pauses_immediately(self):
        pool, _ = self.pool(lambda request: httpx.Response(403))
        with self.assertRaises(WeiboPaused):
            await pool.send("GET", "https://m.weibo.cn/api/config")
        self.assertEqual(pool.stats["requests"], 1)

    async def test_network_errors_are_bounded(self):
        def fail(request):
            raise httpx.ConnectError("offline", request=request)
        pool, _ = self.pool(fail, attempts=2)
        with self.assertRaises(WeiboPaused):
            await pool.send("GET", "https://m.weibo.cn/api/config")
        self.assertEqual(pool.stats["requests"], 2)

    async def test_comment_empty_result_is_not_retried(self):
        client = WeiboClient(headers={}, playwright_page=Mock(), cookie_dict={})
        pool, _ = self.pool(lambda request: httpx.Response(200, json={"ok": 0, "msg": "还没有人评论哦~快来抢沙发！"}))
        client.transport = pool
        result = await client.get_note_all_comments("1", max_count=20, crawl_interval=0)
        self.assertEqual(result, [])
        self.assertEqual(pool.stats["requests"], 1)

    async def test_captcha_body_pauses_without_retry(self):
        client = WeiboClient(headers={}, playwright_page=Mock(), cookie_dict={})
        pool, _ = self.pool(lambda request: httpx.Response(200, json={"ok": -100, "msg": "验证"}))
        client.transport = pool
        with self.assertRaises(WeiboPaused):
            await client.get_note_by_keyword("交通事故")
        self.assertEqual(pool.stats["requests"], 1)

    def test_retry_after_dates_and_empty_scope(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        self.assertGreater(retry_after_seconds(future), 25)
        self.assertIsNone(retry_after_seconds("invalid"))
        self.assertIsNone(retry_after_seconds("nan"))
        self.assertIsNone(empty_result("https://m.weibo.cn/api/config", "没有更多"))

    async def test_consecutive_api_errors_pause(self):
        client = WeiboClient(headers={}, playwright_page=Mock(), cookie_dict={})
        pool, _ = self.pool(lambda request: httpx.Response(200, json={"ok": 0, "msg": "接口错误"}))
        client.transport = pool
        for _ in range(2):
            with self.assertRaises(DataFetchError):
                await client.get_note_by_keyword("交通事故")
        with self.assertRaises(WeiboPaused):
            await client.get_note_by_keyword("交通事故")
        self.assertEqual(pool.stats["requests"], 3)

    async def test_post_comment_restrictions_do_not_pause_other_posts(self):
        for message in ("已过滤部分评论", "博主已开启防火墙，部分内容暂不展示", "博主已开启评论精选"):
            client = WeiboClient(headers={}, playwright_page=Mock(), cookie_dict={})
            pool, _ = self.pool(lambda request: httpx.Response(200, json={"ok": 0, "msg": message}))
            client.transport = pool
            for _ in range(4):
                with self.assertRaises(DataFetchError):
                    await client.get_note_comments("1", -1)
            self.assertEqual(client._api_errors, 0)
        self.assertEqual(empty_result("https://m.weibo.cn/comments/hotflow", "快来发表你的评论吧"),
                         {"data": [], "max_id": 0, "max_id_type": 0})

    async def test_metrics_observation_time_reaches_storage(self):
        from store import weibo

        store = Mock(store_content=AsyncMock())
        item = {"_observed_at": 1700000000123, "mblog": {"id": "a", "text": "交通事故",
                "created_at": "Sat Dec 23 17:12:54 +0800 2023", "user": {}}}
        with patch.object(weibo.WeibostoreFactory, "create_store", return_value=store):
            await weibo.update_weibo_note(item)
        self.assertEqual(store.store_content.await_args.kwargs["content_item"]["last_modify_ts"], 1700000000123)


if __name__ == "__main__":
    unittest.main()
