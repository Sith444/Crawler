"""为平台客户端提供连接复用、统一节流、状态检查和浏览器请求控制。"""

from contextlib import asynccontextmanager

import httpx

import config
from tools.httpx_util import make_async_client
from tools.platform_specs import collection_options
from tools.search_paging import douyin_search_restriction
from tools.weibo_requests import CollectionPaused, WeiboRequests


class BrowserTransport(httpx.AsyncBaseTransport):
    def __init__(self, owner):
        self.owner = owner

    async def handle_async_request(self, request):
        try:
            result = await self.owner.playwright_page.evaluate(
                """async ({url, method, body, headers}) => {
                    const options = {method, credentials: 'include', headers};
                    if (method !== 'GET' && method !== 'HEAD') options.body = body;
                    const response = await fetch(url, options);
                    return {status: response.status, text: await response.text(),
                            retryAfter: response.headers.get('Retry-After')};
                }""",
                {"url": str(request.url), "method": request.method,
                 "body": request.content.decode("utf-8"),
                 "headers": {key: value for key, value in request.headers.items()
                             if key.lower() not in ("cookie", "host", "content-length")}},
            )
        except Exception as exc:
            raise httpx.ConnectError("浏览器请求未完成", request=request) from exc
        headers = {"Retry-After": result["retryAfter"]} if result.get("retryAfter") else {}
        return httpx.Response(result["status"], text=result["text"], headers=headers, request=request)


class SessionView:
    def __init__(self, owner, proxy, defaults):
        self.owner, self.proxy, self.defaults = owner, proxy, defaults

    async def request(self, method, url, **kwargs):
        options = dict(self.defaults, **kwargs)
        if self.owner.collection_platform == "zhihu" and "comment" in str(url):
            options["allowed_statuses"] = (404,)
        if self.owner.collection_platform == "xhs":
            options["allowed_statuses"] = (406,)
        response = await self.owner.request_pool.send(method, url, proxy=self.proxy, **options)
        return self.owner.inspect_collection_response(response)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)


class PlatformHttpMixin:
    collection_platform = ""

    @property
    def request_pool(self):
        if getattr(self, "_collection_pool", None) is None:
            options = collection_options(config, self.collection_platform)
            def factory(proxy):
                if proxy == "__browser__":
                    return httpx.AsyncClient(transport=BrowserTransport(self))
                return make_async_client(proxy=proxy)
            self._collection_pool = WeiboRequests(
                factory, interval=options["interval"], attempts=options["attempts"],
                backoff=options["backoff"], max_wait=options["max_wait"],
                label=self.collection_platform, paused_type=CollectionPaused, login_hosts=(),
            )
        return self._collection_pool

    @asynccontextmanager
    async def http_client(self, **kwargs):
        proxy = kwargs.pop("proxy", getattr(self, "proxy", None))
        yield SessionView(self, proxy, kwargs)

    async def browser_send(self, method, url, **kwargs):
        response = await self.request_pool.send(method, url, proxy="__browser__", **kwargs)
        return self.inspect_collection_response(response)

    async def aclose(self):
        if getattr(self, "_collection_pool", None) is not None:
            await self._collection_pool.close()

    def inspect_collection_response(self, response):
        text = response.text.strip()
        if text == "blocked":
            self.request_pool.pause("平台拒绝当前请求，请检查浏览器验证提示")
        try:
            data = response.json()
        except (ValueError, TypeError):
            if any(word in text[:2000] for word in ("安全验证", "访问过于频繁", "请输入验证码")):
                self.request_pool.pause("页面要求验证或降低请求频率")
            return response
        if not isinstance(data, dict):
            return response
        platform = self.collection_platform
        restriction = douyin_search_restriction(data) if platform == "dy" else None
        if restriction:
            self.request_pool.pause(f"抖音搜索返回验证或访问限制标记 {restriction}，不是无搜索结果；请在 Chrome 搜索页面处理提示后重新运行")
        error = data.get("error")
        error_message = error.get("message", "") if isinstance(error, dict) else ""
        message = str(data.get("msg") or data.get("message") or data.get("error_msg") or data.get("status_msg") or error_message)
        if any(word in message for word in ("验证码", "安全验证", "访问频繁", "操作频繁", "访问受限")):
            self.request_pool.pause("接口要求验证或降低请求频率")
        if platform == "xhs" and str(data.get("code")) in {
            str(getattr(self, "IP_ERROR_CODE", "__none__")), str(getattr(self, "SECURITY_LIMIT_CODE", "__none__"))
        }:
            self.request_pool.pause("平台返回访问限制，请检查浏览器页面")
        if platform == "ks" and data.get("result") == 2:
            self.request_pool.pause("平台返回限流状态，请稍后再继续")
        if any(word in message for word in ("暂无评论", "没有评论", "评论已关闭")) and any(
            word in str(response.url) for word in ("comment", "reply")
        ):
            empty = {
                "bili": {"code": 0, "data": {"cursor": {"is_end": True, "next": 0}, "replies": []}},
                "xhs": {"success": True, "code": 0, "data": {"comments": [], "has_more": False, "cursor": ""}},
                "dy": {"status_code": 0, "comments": [], "has_more": 0, "cursor": 0},
                "ks": {"result": 1, "rootCommentsV2": [], "pcursorV2": "no_more"},
                "zhihu": {"data": [], "paging": {"is_end": True}},
            }.get(platform)
            if empty is not None:
                self._collection_api_errors = 0
                return httpx.Response(200, json=empty, request=response.request)
        failed = ((platform == "bili" and data.get("code", 0) != 0) or
                  (platform == "xhs" and data.get("success") is False) or
                  (platform == "dy" and data.get("status_code", 0) != 0) or
                  (platform == "ks" and (bool(data.get("errors")) or data.get("result", 1) != 1)) or
                  (platform == "zhihu" and bool(data.get("error"))) or
                  (platform == "tieba" and str(data.get("error_code", data.get("no", 0))) not in ("0", "")))
        unavailable_video = (
            platform == "bili"
            and response.request.url.path == "/x/web-interface/view/detail"
            and message in ("啥都木有", "稿件不可见")
        )
        if failed and not unavailable_video and not any(word in message for word in ("不存在", "已删除", "不见了")):
            self._collection_api_errors = getattr(self, "_collection_api_errors", 0) + 1
            if self._collection_api_errors >= 3:
                self.request_pool.pause("连续三次接口错误，请检查平台状态")
        elif not failed:
            self._collection_api_errors = 0
        return response
