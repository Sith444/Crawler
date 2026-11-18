# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:40
# @Desc    : Weibo crawler API request client

import asyncio
import copy
import json
import re
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union
from urllib.parse import parse_qs, unquote, urlencode

import httpx
from httpx import Response
from playwright.async_api import BrowserContext, Page
from tools.httpx_util import make_async_client
from tools.weibo_requests import WeiboRequests, empty_result

import config
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import DataFetchError
from .field import SearchType


class WeiboClient(ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Weibo images need a longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://m.weibo.cn"
        self.cookie_urls = [self._host]
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._api_errors = 0
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)
        self.transport = WeiboRequests(
            lambda proxy: make_async_client(proxy=proxy, follow_redirects=True),
            interval=config.CRAWLER_MAX_SLEEP_SEC,
            attempts=config.COLLECTION_REQUEST_ATTEMPTS,
            backoff=config.COLLECTION_RETRY_BACKOFF_SEC,
            max_wait=config.COLLECTION_MAX_RETRY_WAIT_SEC,
        )

    async def request(self, method, url, **kwargs) -> Union[Response, Dict]:
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        enable_return_response = kwargs.pop("return_response", False)
        response = await self.transport.send(method, url, proxy=self.proxy, timeout=self.timeout, **kwargs)

        if enable_return_response:
            return response

        try:
            data: Dict = response.json()
        except json.decoder.JSONDecodeError:
            self.transport.pause("接口未返回 JSON，可能需要登录或安全验证，请检查浏览器页面")

        if not isinstance(data, dict):
            raise DataFetchError("Unexpected API response structure")
        message = str(data.get("msg") or data.get("message") or "")
        if any(str(data.get(key)) == "-100" for key in ("ok", "errno", "code")) or any(
            word in message for word in ("验证码", "访问频繁", "操作频繁", "登录失效", "请先登录", "安全验证")
        ):
            self.transport.pause("平台要求验证、重新登录或降低请求频率，请检查浏览器页面")
        ok_code = data.get("ok")
        if ok_code == 0:  # response error
            result = empty_result(url, message)
            if result is not None:
                self._api_errors = 0
                return result
            if "/comments/hotflow" in url and message in (
                "已过滤部分评论", "博主已开启防火墙，部分内容暂不展示", "博主已开启评论精选",
            ):
                self._api_errors = 0
                raise DataFetchError(message)
            self._api_errors += 1
            if self._api_errors >= 3:
                self.transport.pause("连续三次接口错误，请检查平台状态后再继续")
            raise DataFetchError(message or "response error")
        elif ok_code != 1:  # unknown error
            self._api_errors += 1
            if self._api_errors >= 3:
                self.transport.pause("连续三次接口错误，请检查平台状态后再继续")
            raise DataFetchError(data.get("msg", "unknown error"))
        else:  # response right
            self._api_errors = 0
            return data.get("data", {})

    async def aclose(self):
        await self.transport.close()

    async def get(self, uri: str, params=None, headers=None, **kwargs) -> Union[Response, Dict]:
        final_uri = uri
        if isinstance(params, dict):
            final_uri = (f"{uri}?"
                         f"{urlencode(params)}")

        if headers is None:
            headers = self.headers
        return await self.request(method="GET", url=f"{self._host}{final_uri}", headers=headers, **kwargs)

    async def post(self, uri: str, data: dict) -> Dict:
        json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        return await self.request(method="POST", url=f"{self._host}{uri}", data=json_str, headers=self.headers)

    async def pong(self) -> bool:
        """get a note to check if login state is ok"""
        utils.logger.info("[WeiboClient.pong] Begin pong weibo...")
        ping_flag = False
        try:
            uri = "/api/config"
            resp_data: Dict = await self.request(method="GET", url=f"{self._host}{uri}", headers=self.headers)
            if resp_data.get("login"):
                ping_flag = True
            else:
                utils.logger.error(f"[WeiboClient.pong] cookie may be invalid and again login...")
        except Exception as e:
            utils.logger.error(f"[WeiboClient.pong] Pong weibo failed: {e}, and try to login again...")
            ping_flag = False
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[List[str]] = None):
        """
        Update cookies from browser context
        :param browser_context: Browser context
        :param urls: Optional list of URLs to filter cookies (e.g., ["https://m.weibo.cn"])
                     If provided, only cookies for these URLs will be retrieved
        """
        cookie_urls = urls or self.cookie_urls
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict
        utils.logger.info(
            f"[WeiboClient.update_cookies] Cookie updated successfully for {cookie_urls}, total: {len(cookie_dict)} cookies"
        )

    async def get_note_by_keyword(
        self,
        keyword: str,
        page: int = 1,
        search_type: SearchType = SearchType.DEFAULT,
    ) -> Dict:
        """
        search note by keyword
        :param keyword: Search keyword for Weibo
        :param page: Pagination parameter - current page number
        :param search_type: Search type, see SearchType enum in weibo/field.py
        :return:
        """
        uri = "/api/container/getIndex"
        containerid = f"100103type={search_type.value}&q={keyword}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
        return await self.get(uri, params)

    async def get_note_comments(self, mid_id: str, max_id: int, max_id_type: int = 0) -> Dict:
        """get notes comments
        :param mid_id: Weibo ID
        :param max_id: Pagination parameter ID
        :param max_id_type: Pagination parameter ID type
        :return:
        """
        uri = "/comments/hotflow"
        params = {
            "id": mid_id,
            "mid": mid_id,
            "max_id_type": max_id_type,
        }
        if max_id > 0:
            params.update({"max_id": max_id})
        referer_url = f"https://m.weibo.cn/detail/{mid_id}"
        headers = copy.copy(self.headers)
        headers["Referer"] = referer_url

        return await self.get(uri, params, headers=headers)

    async def get_note_all_comments(
        self,
        note_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ):
        """
        get note all comments include sub comments
        :param note_id:
        :param crawl_interval:
        :param callback:
        :param max_count:
        :return:
        """
        result = []
        is_end = False
        max_id = -1
        max_id_type = 0
        while not is_end and len(result) < max_count:
            comments_res = await self.get_note_comments(note_id, max_id, max_id_type)
            max_id: int = comments_res.get("max_id")
            max_id_type: int = comments_res.get("max_id_type")
            comment_list: List[Dict] = comments_res.get("data", [])
            is_end = not comment_list or max_id in (0, None)
            if len(result) + len(comment_list) > max_count:
                comment_list = comment_list[:max_count - len(result)]
            if callback:  # If callback function exists, execute it
                await callback(note_id, comment_list)
            await asyncio.sleep(crawl_interval)
            result.extend(comment_list)
            sub_comment_result = await self.get_comments_all_sub_comments(note_id, comment_list, callback)
            result.extend(sub_comment_result)
        return result

    @staticmethod
    async def get_comments_all_sub_comments(
        note_id: str,
        comment_list: List[Dict],
        callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all sub-comments of comments
        Args:
            note_id:
            comment_list:
            callback:

        Returns:

        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(f"[WeiboClient.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled")
            return []

        res_sub_comments = []
        for comment in comment_list:
            sub_comments = comment.get("comments")
            if sub_comments and isinstance(sub_comments, list):
                await callback(note_id, sub_comments)
                res_sub_comments.extend(sub_comments)
        return res_sub_comments

    async def get_note_info_by_id(self, note_id: str) -> Dict:
        """
        Get note details by note ID
        :param note_id:
        :return:
        """
        url = f"{self._host}/detail/{note_id}"
        response = await self.request("GET", url, headers=self.headers, return_response=True)
        match = re.search(r'var \$render_data = (\[.*?\])\[0\]', response.text, re.DOTALL)
        if match:
            render_data_dict = json.loads(match.group(1))
            return {"mblog": render_data_dict[0].get("status")}
        raise DataFetchError("Detail page did not contain post content")


    async def get_creator_container_info(self, creator_id: str) -> Dict:
        """
        Get user's container ID, container information represents the real API request path
            fid_container_id: Container ID for user's Weibo detail API
            lfid_container_id: Container ID for user's Weibo list API
        Args:
            creator_id: User ID

        Returns: Dictionary with container IDs

        """
        response = await self.get(f"/u/{creator_id}", return_response=True)
        m_weibocn_params = response.cookies.get("M_WEIBOCN_PARAMS")
        if not m_weibocn_params:
            raise DataFetchError("get containerid failed")
        m_weibocn_params_dict = parse_qs(unquote(m_weibocn_params))
        return {"fid_container_id": m_weibocn_params_dict.get("fid", [""])[0], "lfid_container_id": m_weibocn_params_dict.get("lfid", [""])[0]}

    async def get_creator_info_by_id(self, creator_id: str) -> Dict:
        """
        Get user details by user ID
        Args:
            creator_id:

        Returns:

        """
        uri = "/api/container/getIndex"
        containerid = f"100505{creator_id}"
        params = {
            "jumpfrom": "weibocom",
            "type": "uid",
            "value": creator_id,
            "containerid":containerid,
        }
        user_res = await self.get(uri, params)
        return user_res

    async def get_notes_by_creator(
        self,
        creator: str,
        container_id: str,
        since_id: str = "0",
    ) -> Dict:
        """
        Get creator's notes
        Args:
            creator: Creator ID
            container_id: Container ID
            since_id: ID of the last note from previous page
        Returns:

        """

        uri = "/api/container/getIndex"
        params = {
            "jumpfrom": "weibocom",
            "type": "uid",
            "value": creator,
            "containerid": container_id,
            "since_id": since_id,
        }
        return await self.get(uri, params)

    async def get_all_notes_by_creator_id(
        self,
        creator_id: str,
        container_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all posts published by a specified user, this method will continuously fetch all posts from a user
        Args:
            creator_id: Creator user ID
            container_id: Container ID for the user
            crawl_interval: Interval between requests in seconds
            callback: Optional callback function to process notes

        Returns: List of all notes

        """
        result = []
        notes_has_more = True
        since_id = ""
        crawler_total_count = 0
        while notes_has_more:
            notes_res = await self.get_notes_by_creator(creator_id, container_id, since_id)
            if not notes_res:
                utils.logger.error(f"[WeiboClient.get_notes_by_creator] The current creator may have been banned by Weibo, so they cannot access the data.")
                break
            since_id = notes_res.get("cardlistInfo", {}).get("since_id", "0")
            if "cards" not in notes_res:
                utils.logger.info(f"[WeiboClient.get_all_notes_by_creator] No 'notes' key found in response: {notes_res}")
                break

            notes = notes_res["cards"]
            utils.logger.info(f"[WeiboClient.get_all_notes_by_creator] got user_id:{creator_id} notes len : {len(notes)}")
            notes = [note for note in notes if note.get("card_type") == 9]
            if callback:
                await callback(notes)
            await asyncio.sleep(crawl_interval)
            result.extend(notes)
            crawler_total_count += 10
            notes_has_more = notes_res.get("cardlistInfo", {}).get("total", 0) > crawler_total_count
        return result
