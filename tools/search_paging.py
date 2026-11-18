"""验证搜索分页响应、复查无分页标记的空页并保留异常断点。"""

import asyncio
from dataclasses import dataclass
import logging

from tools.weibo_requests import CollectionPaused


class SearchResponseError(ValueError):
    """响应缺少必要字段或分页信息异常，可以有限重试。"""


def douyin_search_restriction(data):
    """识别搜索空结果附带的验证、登录和访问限制标记。"""
    if not isinstance(data, dict):
        return None
    info = data.get("search_nil_info")
    if not info:
        return None
    markers = str(info).lower()
    for marker in ("verify_check", "captcha", "risk_check", "login_check", "not_login", "frequency", "rate_limit"):
        if marker in markers:
            return marker
    return None


@dataclass
class SearchPage:
    contexts: list
    cursor: dict
    finished: bool = False
    continue_empty: bool = False
    unconfirmed_empty: bool = False
    finish_reason: str = ""


def page_flag(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    raise SearchResponseError("分页标记格式异常")


async def fetch_checked(fetch, options, label, *, paused_type=CollectionPaused):
    """同一页异常时有限重试；未确认的正常空列表须复查后才结束翻页。"""
    attempts = max(2, int(options["attempts"]))
    empty_count = 0
    for attempt in range(attempts):
        try:
            page = await fetch()
            if not page.unconfirmed_empty:
                return page
            empty_count += 1
            if empty_count >= 2:
                page.finished = True
                page.finish_reason = "同一页连续两次返回正常空列表"
                return page
            message = "空页未附结束标记，复查当前页"
        except SearchResponseError as exc:
            message = str(exc)
            empty_count = 0
        if attempt + 1 >= attempts:
            reason = f"{label} 连续 {attempts} 次未取得有效分页响应：{message}；已保留断点"
            logging.getLogger("MediaCrawler").warning("[SearchPaging] %s", reason)
            raise paused_type(reason)
        wait = min(options["max_wait"], options["backoff"] * 2 ** attempt)
        logging.getLogger("MediaCrawler").warning("[SearchPaging] %s：%s，%s 秒后重试当前页 (%s/%s)", label, message, wait, attempt + 1, attempts)
        await asyncio.sleep(wait)
