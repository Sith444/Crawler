"""将平台原生搜索、详情和评论接口适配为统一的关键词采集流程。"""

import asyncio
from contextlib import closing
from dataclasses import dataclass
import importlib
from pathlib import Path
import sqlite3
import time

from tools.platform_specs import PLATFORMS
from tools.traffic_filter import clean_text
from tools.weibo_requests import CollectionPaused
from tools.search_paging import SearchPage, SearchResponseError, page_flag, douyin_search_restriction


@dataclass
class Content:
    key: str
    context: dict
    text: str
    data: object = None
    observed_at: int = 0
    cached: bool = False


class PlatformAdapter:
    def __init__(self, platform, crawler, settings):
        self.platform, self.crawler, self.settings = platform, crawler, settings
        self.spec = PLATFORMS[platform]
        self.client = getattr(crawler, self.spec.client)
        module = {"bili": "bilibili", "dy": "douyin", "ks": "kuaishou"}.get(platform, platform)
        self.store = importlib.import_module(f"store.{module}")
        self.memory = {}

    def key(self, context):
        if self.platform == "zhihu":
            return f"{context['model']['content_id']}:{context['model']['content_type']}"
        return str(context["id"])

    async def fetch(self, keyword, page, cursor):
        client, platform = self.client, self.platform
        if platform == "bili":
            from media_platform.bilibili.field import SearchOrderType

            order = SearchOrderType(getattr(self.settings, "BILI_SEARCH_ORDER", ""))
            result = await client.search_video_by_keyword(keyword=keyword, page=page, page_size=20, order=order)
            if not isinstance(result, dict) or not isinstance(result.get("result", []), list):
                raise SearchResponseError("B站搜索结果结构异常")
            if "result" not in result and result.get("numResults") != 0 and result.get("numPages") != 0:
                raise SearchResponseError("B站搜索缺少结果列表与结束标记")
            rows = [{"id": str(item["aid"])} for item in result.get("result", []) if item.get("aid")]
            try:
                total_pages = int(result["numPages"]) if result.get("numPages") is not None else None
                if total_pages is not None and total_pages < 0:
                    raise ValueError("negative page count")
            except (ValueError, TypeError):
                raise SearchResponseError("B站总页数格式异常")
            finished = total_pages is not None and page >= total_pages
            raw_items = result.get("result", [])
            return SearchPage(rows, {}, finished, continue_empty=not finished,
                              unconfirmed_empty=not raw_items and total_pages is None)
        if platform == "xhs":
            from media_platform.xhs.field import SearchSortType

            sort = SearchSortType(self.settings.SORT_TYPE) if self.settings.SORT_TYPE else SearchSortType.GENERAL
            from media_platform.xhs.help import get_search_id

            search_id = cursor.get("search_id") or get_search_id()
            result = await client.get_note_by_keyword(keyword=keyword, page=page, sort=sort, search_id=search_id)
            if not isinstance(result, dict) or not isinstance(result.get("items", []), list):
                raise SearchResponseError("小红书搜索结果结构异常")
            more = page_flag(result.get("has_more"))
            if "items" not in result and more is not False:
                raise SearchResponseError("小红书搜索缺少笔记列表与结束标记")
            rows = [{"id": str(item["id"]), "xsec_source": item.get("xsec_source", ""),
                     "xsec_token": item.get("xsec_token", "")} for item in result.get("items", [])
                    if item.get("id") and item.get("model_type") not in ("rec_query", "hot_query")]
            return SearchPage(rows, {"search_id": search_id, "page": page + 1}, more is False,
                              continue_empty=more is not False,
                              unconfirmed_empty=not result.get("items") and more is None)
        if platform == "dy":
            from media_platform.douyin.field import PublishTimeType, SearchSortType

            result = await client.search_info_by_keyword(keyword=keyword, offset=cursor.get("offset", (page - 1) * 15),
                                                        search_id=cursor.get("search_id", ""),
                                                        sort_type=SearchSortType(getattr(self.settings, "DY_SEARCH_SORT_TYPE", 0)),
                                                        publish_time=PublishTimeType(self.settings.PUBLISH_TIME_TYPE))
            restriction = douyin_search_restriction(result)
            if restriction:
                raise CollectionPaused(f"抖音搜索返回 {restriction}，需要在 Chrome 搜索页处理验证或登录提示，已保留断点")
            if not isinstance(result, dict) or str(result.get("status_code", 0)) != "0":
                raise CollectionPaused("抖音搜索返回异常状态，未将关键词标记为完成，请检查平台提示")
            items = result.get("data")
            has_more = result.get("has_more")
            finished = has_more is not None and str(has_more) in ("0", "False")
            if items is not None and not isinstance(items, list):
                raise CollectionPaused("抖音搜索结果结构异常，已保留当前断点")
            if not items and not finished and str(has_more) not in ("1", "True"):
                raise CollectionPaused("抖音搜索返回空响应且未说明是否还有结果，已保留断点，请检查登录或验证提示")
            rows = []
            for item in items or []:
                data = item.get("aweme_info")
                if not data:
                    mix = (item.get("aweme_mix_info") or {}).get("mix_items") or []
                    data = mix[0] if mix else None
                if data and data.get("aweme_id"):
                    rows.append({"id": str(data["aweme_id"]), "data": data})
            offset = cursor.get("offset", (page - 1) * 15)
            try:
                next_offset = int(result.get("cursor", offset + 15))
                if next_offset < 0:
                    raise ValueError("negative offset")
            except (TypeError, ValueError):
                raise CollectionPaused("抖音搜索游标异常，已保留当前断点")
            if not finished and next_offset <= offset:
                raise CollectionPaused("抖音搜索仍有更多内容但游标未前进，已保留断点")
            next_cursor = {"offset": next_offset,
                           "search_id": result.get("search_id") or (result.get("extra") or {}).get("logid") or cursor.get("search_id", "")}
            return SearchPage(rows, next_cursor, finished, continue_empty=not finished)
        if platform == "ks":
            result = await client.search_info_by_keyword_v2(keyword, cursor.get("pcursor", ""), cursor.get("search_id", ""))
            if not isinstance(result, dict) or not isinstance(result.get("feeds", []), list):
                raise SearchResponseError("快手搜索结果结构异常")
            if "pcursor" not in result or result["pcursor"] is None:
                raise SearchResponseError("快手搜索未返回分页游标，不能判定结束")
            next_token = str(result["pcursor"])
            finished = next_token in ("no_more", "")
            if "feeds" not in result and not finished:
                raise SearchResponseError("快手搜索缺少作品列表")
            if not finished and next_token == str(cursor.get("pcursor", "")):
                raise SearchResponseError("快手搜索分页游标未前进")
            rows = [{"id": str(item["photo"]["id"]), "data": item} for item in result.get("feeds") or []
                    if (item.get("photo") or {}).get("id")]
            next_cursor = {"pcursor": next_token, "search_id": result.get("searchSessionId") or cursor.get("search_id", "")}
            return SearchPage(rows, next_cursor, finished, continue_empty=not finished)
        if platform == "tieba":
            result = await client.get_notes_by_keyword(keyword=keyword, page=page, page_size=20, with_paging=True)
            more = page_flag(result.get("has_more"))
            rows = result["contents"]
            return SearchPage([{"id": item.note_id, "model": item.model_dump()} for item in rows], {}, more is False,
                              continue_empty=more is not False,
                              unconfirmed_empty=result["raw_count"] == 0 and more is None)
        if platform == "zhihu":
            from urllib.parse import parse_qs, urlparse

            offset = cursor.get("offset", (page - 1) * 20)
            result = await client.get_note_by_keyword(keyword=keyword, page=page, page_size=20, offset=offset, with_paging=True)
            rows, paging = result["contents"], result["paging"]
            finished = page_flag(paging.get("is_end"))
            try:
                next_offset = int(parse_qs(urlparse(paging.get("next") or "").query).get("offset", [offset + 20])[0])
            except (TypeError, ValueError):
                raise SearchResponseError("知乎搜索下一页偏移量异常")
            if finished is not True and next_offset <= offset:
                raise SearchResponseError("知乎搜索分页偏移量未前进")
            return SearchPage([{"id": item.content_id, "model": item.model_dump()} for item in rows
                               if item.content_id and item.content_type in ("answer", "article", "zvideo")],
                              {"offset": next_offset}, finished is True, continue_empty=finished is not True,
                              unconfirmed_empty=result["raw_count"] == 0 and finished is None)
        raise ValueError(platform)

    def task_context(self, context):
        # 视频搜索响应只在内存中使用；补采记录只保留内容标识和必要参数。
        return {key: value for key, value in context.items() if key != "data"}

    def cached_content(self, context, state, hours):
        if self.platform in ("dy", "ks"):
            return None
        key = self.key(context)
        text = state.cached_detail(key, hours)
        if text is None:
            return None
        if key not in self.memory:
            path = Path(self.settings.SQLITE_DB_PATH)
            if self.settings.SAVE_DATA_OPTION != "sqlite" or not path.is_file():
                return None
            values = tuple(context["model"][column] for column in self.spec.keys) if self.platform == "zhihu" else (context["id"],)
            where = " AND ".join(f'"{column}"=?' for column in self.spec.keys)
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute(f'SELECT id FROM "{self.spec.posts}" WHERE {where}', values).fetchone()
            if row is None:
                return None
        saved_context = state.context(key) or self.task_context(context)
        if self.platform == "xhs":
            saved_context = self.task_context(context)
        return Content(key, saved_context, text, cached=True)

    async def load(self, context):
        platform, client = self.platform, self.client
        if platform == "bili":
            data = await client.get_video_info(aid=context["id"])
            view = data.get("View") or {}
            if not view.get("aid"):
                raise ValueError("视频详情缺少标识")
            text = " ".join((view.get("title", ""), view.get("desc", "")))
        elif platform == "xhs":
            data = await client.get_note_by_id(context["id"], context.get("xsec_source", ""), context.get("xsec_token", ""))
            if not data or not data.get("note_id"):
                raise ValueError("笔记详情缺少标识")
            data["xsec_token"] = context.get("xsec_token", "")
            text = " ".join((data.get("title", ""), data.get("desc", "")))
        elif platform == "dy":
            data = context.get("data")
            if data is None:
                data = await client.get_video_by_id(context["id"])
            if not data or not data.get("aweme_id"):
                raise ValueError("视频详情缺少标识")
            text = data.get("desc", "")
        elif platform == "ks":
            data = context.get("data")
            if data is None:
                result = await client.get_video_info(context["id"])
                data = result.get("visionVideoDetail") or result
            if not (data.get("photo") or {}).get("id"):
                raise ValueError("视频详情缺少标识")
            text = data["photo"].get("caption", "")
        elif platform == "tieba":
            data = await client.get_note_by_id(context["id"])
            if data is None:
                raise ValueError("帖子详情为空")
            context = {"id": data.note_id, "model": data.model_dump()}
            text = data.title + " " + data.desc
        else:
            from model.m_zhihu import ZhihuContent

            data = ZhihuContent.model_validate(context["model"])
            if not data.content_text:
                if data.content_type == "answer":
                    data = await client.get_answer_info(data.question_id, data.content_id)
                elif data.content_type == "article":
                    data = await client.get_article_info(data.content_id)
                else:
                    data = await client.get_video_info(data.content_id)
            if data is None:
                raise ValueError("内容详情为空")
            context = {"id": data.content_id, "model": data.model_dump()}
            text = " ".join((data.title, data.content_text, data.desc))
        observed_at = context.get("observed_at") if context.get("data") is not None else None
        return Content(self.key(context), self.task_context(context), clean_text(text), data,
                       observed_at or int(time.time() * 1000))

    async def save(self, content):
        from var import metrics_observed_at_var

        token = metrics_observed_at_var.set(content.observed_at)
        try:
            updater = {"bili": "update_bilibili_video", "xhs": "update_xhs_note", "dy": "update_douyin_aweme",
                       "ks": "update_kuaishou_video", "tieba": "update_tieba_note", "zhihu": "update_zhihu_content"}[self.platform]
            await getattr(self.store, updater)(content.data)
        finally:
            metrics_observed_at_var.reset(token)
        self.memory[content.key] = content.text
        if self.settings.ENABLE_GET_MEDIA and hasattr(self.crawler, "download_media"):
            if self.platform == "bili":
                await self.crawler.download_media(content.data, asyncio.Semaphore(1))
            else:
                await self.crawler.download_media(content.data)

    async def comments(self, content):
        client, store, context = self.client, self.store, content.context
        options = dict(crawl_interval=0, max_count=self.settings.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES)
        if self.platform == "bili":
            return await client.get_video_all_comments(video_id=context["id"], callback=store.batch_update_bilibili_video_comments,
                                                       is_fetch_sub_comments=self.settings.ENABLE_GET_SUB_COMMENTS, **options)
        if self.platform == "xhs":
            return await client.get_note_all_comments(note_id=context["id"], xsec_token=context.get("xsec_token", ""), callback=store.batch_update_xhs_note_comments, **options)
        if self.platform == "dy":
            return await client.get_aweme_all_comments(aweme_id=context["id"], callback=store.batch_update_dy_aweme_comments,
                                                       is_fetch_sub_comments=self.settings.ENABLE_GET_SUB_COMMENTS, **options)
        if self.platform == "ks":
            return await client.get_video_all_comments(photo_id=context["id"], callback=store.batch_update_ks_video_comments, **options)
        if self.platform == "tieba":
            from model.m_baidu_tieba import TiebaNote

            return await client.get_note_all_comments(note_detail=TiebaNote.model_validate(context["model"]),
                                                      callback=store.batch_update_tieba_note_comments, **options)
        from model.m_zhihu import ZhihuContent

        return await client.get_note_all_comments(content=ZhihuContent.model_validate(context["model"]),
                                                  callback=store.batch_update_zhihu_note_comments, **options)
