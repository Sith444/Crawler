"""复用微博 HTTP 连接，统一请求间隔、有限重试及暂停条件。"""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import time

import httpx


class WeiboPaused(asyncio.CancelledError):
    """遇到访问限制或登录失效时暂停采集，交由用户处理。"""


class CollectionPaused(asyncio.CancelledError):
    """平台要求验证、登录或停止请求时暂停当前采集。"""


def retry_after_seconds(value):
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class WeiboRequests:
    def __init__(self, factory, *, interval=0.5, attempts=3, backoff=2, max_wait=60,
                 label="Weibo", paused_type=WeiboPaused, login_hosts=("passport.weibo",)):
        self.factory = factory
        self.interval = max(0, interval)
        self.attempts = max(1, attempts)
        self.backoff = max(0, backoff)
        self.max_wait = max_wait
        self.client = None
        self.proxy = None
        self.lock = asyncio.Lock()
        self.next_request = 0
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self.stats = dict(requests=0, retries=0, rate_limits=0)
        self.label, self.paused_type, self.login_hosts = label, paused_type, login_hosts

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def pause(self, reason):
        logging.getLogger("MediaCrawler").warning("[%sRequest] 采集暂停：%s", self.label, reason)
        raise self.paused_type(reason)

    async def send(self, method, url, *, proxy=None, **kwargs):
        allowed_statuses = kwargs.pop("allowed_statuses", ())
        async with self.lock:
            if self.client is None or proxy != self.proxy:
                await self.close()
                self.client = self.factory(proxy)
                self.proxy = proxy
            for attempt in range(self.attempts):
                await self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + self.interval
                self.stats["requests"] += 1
                wait = None
                try:
                    response = await self.client.request(method, url, **kwargs)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                    wait = self.backoff * 2 ** attempt
                else:
                    if response.status_code in (401, 403, 418, 432, 461, 471):
                        self.pause(f"HTTP {response.status_code}，请检查登录状态或验证提示")
                    if any(host in str(response.url) for host in self.login_hosts):
                        self.pause("接口跳转到登录页面，请重新登录")
                    if response.status_code == 429:
                        self.stats["rate_limits"] += 1
                        wait = retry_after_seconds(response.headers.get("Retry-After"))
                        if wait is None:
                            wait = self.backoff * 2 ** attempt
                    elif response.status_code >= 500:
                        wait = self.backoff * 2 ** attempt
                    else:
                        if response.status_code not in allowed_statuses:
                            response.raise_for_status()
                        return response
                if attempt + 1 >= self.attempts:
                    self.pause("连续请求失败或受到限流，已停止自动重试")
                if wait > self.max_wait:
                    self.pause(f"服务端要求等待约 {wait:.0f} 秒，请稍后再运行")
                self.stats["retries"] += 1
                logging.getLogger("MediaCrawler").info("[%sRequest] 等待 %.1f 秒后重试 (%s/%s)", self.label, wait, attempt + 1, self.attempts)
                await self.sleep(wait)


def empty_result(url, message):
    """将明确的无内容提示转换为正常的空结果。"""
    if "/comments/hotflow" in url and any(word in message for word in ("还没有人评论", "暂无评论", "没有更多评论", "快来发表你的评论吧")):
        return {"data": [], "max_id": 0, "max_id_type": 0}
    if "/api/container/getIndex" in url and "page_type=searchall" in url:
        if any(word in message for word in ("这里还没有内容", "暂无搜索结果", "没有更多", "未找到相关")):
            return {"cards": [], "_empty_confirmed": True}
    return None
