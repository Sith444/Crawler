"""多平台关键词采集：按平台隔离断点、缓存、补采记录和周期快照。"""

import asyncio
from pathlib import Path
import time

from tools.collection_state import CollectionState
from tools.platform_adapters import PlatformAdapter
from tools.platform_specs import collection_options
from tools.weibo_requests import CollectionPaused
from tools.search_paging import fetch_checked
from tools.weibo_search import needs_comments
from tools import utils


class CollectionSearch:
    def __init__(self, platform, crawler, settings, *, adapter=None):
        self.platform, self.settings = platform, settings
        self.options = collection_options(settings, platform)
        self.adapter = adapter or PlatformAdapter(platform, crawler, settings)
        root = Path(settings.SAVE_DATA_PATH) if settings.SAVE_DATA_PATH else Path(__file__).resolve().parent.parent / "data"
        self.state = CollectionState(root / platform / "collection_state.db")
        self.attempted_comments = set()
        self.snapshot_posts = 0
        self.snapshot_at = time.monotonic()
        self.stats = dict(saved=0, cache_hits=0, comment_requests=0, comment_skips=0, failures=0)

    async def process(self, context):
        key = self.adapter.key(context)
        content = self.adapter.cached_content(context, self.state, self.options["detail_hours"])
        if content is not None:
            self.stats["cache_hits"] += 1
            self.state.save_context(key, content.context)
            return content
        self.state.save_context(key, self.adapter.task_context(context))
        try:
            content = await self.adapter.load(context)
            await self.adapter.save(content)
            self.state.save_context(key, content.context)
            self.state.save_detail(key, content.text)
            self.stats["saved"] += 1
            self.snapshot_posts += 1
            return content
        except Exception as exc:
            self.state.fail("detail", key, exc)
            self.stats["failures"] += 1
            utils.logger.warning("[CollectionSearch] platform=%s key=%s 内容待补采：%s", self.platform, key, exc)
            return None

    async def comments(self, content):
        if not self.settings.ENABLE_GET_COMMENTS:
            return
        key, limit = content.key, self.settings.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES
        if (limit <= 0 or key in self.attempted_comments or
                self.state.comments_fresh(key, self.options["comment_hours"], limit) or
                (self.options["staged_comments"] and not needs_comments(
                    content.text, self.settings.TRAFFIC_MIN_CONTENT_LEN, self.settings.TRAFFIC_MIN_EVENT_DIMENSIONS))):
            self.stats["comment_skips"] += 1
            return
        self.attempted_comments.add(key)
        self.stats["comment_requests"] += 1
        try:
            await self.adapter.comments(content)
            self.state.save_comments(key, limit)
        except Exception as exc:
            self.state.fail("comments", key, exc)
            self.stats["failures"] += 1
            utils.logger.warning("[CollectionSearch] platform=%s key=%s 评论待补采：%s", self.platform, key, exc)

    async def snapshot(self):
        if self.settings.SAVE_DATA_OPTION != "sqlite" or not (self.settings.SQLITE_EXPORT_EXCEL or self.settings.SQLITE_EXPORT_JSONL):
            return
        posts_due = self.options["snapshot_posts"] > 0 and self.snapshot_posts >= self.options["snapshot_posts"]
        time_due = self.options["snapshot_seconds"] > 0 and time.monotonic() - self.snapshot_at >= self.options["snapshot_seconds"]
        if posts_due or time_due:
            from tools.sqlite_exporter import export_from_config

            try:
                await asyncio.to_thread(export_from_config)
                self.state.resolve("snapshot", "export")
            except Exception as exc:
                self.state.fail("snapshot", "export", exc)
                utils.logger.warning("[CollectionSearch] platform=%s 快照失败：%s", self.platform, exc)
            self.snapshot_posts = 0
            self.snapshot_at = time.monotonic()

    async def run(self):
        """按关键词持续向后翻页，取消或暂停时保留当前断点。"""
        try:
            if not any(word.strip() for word in self.settings.KEYWORDS.split(",")):
                raise ValueError("Search keywords must not be empty")
            await self._search_keywords()
            await self.snapshot()
        except CollectionPaused as exc:
            utils.logger.warning("[CollectionSearch] platform=%s 采集暂停：%s", self.platform, exc)
            raise
        finally:
            self.state.close()

    async def _search_keywords(self):
        from var import request_keyword_var, source_keyword_var

        maximum, start = self.settings.CRAWLER_MAX_NOTES_COUNT, self.settings.START_PAGE
        if maximum < 0 or start < 1:
            raise ValueError("Invalid search start page or maximum")
        keywords = list(dict.fromkeys(word.strip() for word in self.settings.KEYWORDS.split(",") if word.strip()))
        search_mode = f"{self.platform}:{getattr(self.settings, 'SORT_TYPE', '')}:{getattr(self.settings, 'PUBLISH_TIME_TYPE', '')}"
        if self.platform == "dy":
            search_mode += f":dy_sort={getattr(self.settings, 'DY_SEARCH_SORT_TYPE', 0)}"
        if self.platform == "bili":
            search_mode += f":bili_order={getattr(self.settings, 'BILI_SEARCH_ORDER', '')}"
        scope = self.state.begin(keywords, search_mode, start, maximum, self.options["resume"])
        pending = list(dict.fromkeys(self.state.failed("detail") + self.state.failed("comments")))
        for key in pending:
            context = self.state.context(key)
            if context is not None:
                source_keyword_var.set(self.state.keyword(key))
                request_keyword_var.set(self.state.keyword(key))
                content = await self.process(context)
                if content is not None:
                    await self.comments(content)
                await self.snapshot()
        for keyword in keywords:
            source_keyword_var.set(keyword)
            request_keyword_var.set(keyword)
            page, collected, completed = self.state.progress(scope, keyword)
            if completed:
                continue
            cursor, seen = self.state.cursor(scope, keyword), set()
            while maximum == 0 or collected < maximum:
                utils.logger.info("[CollectionSearch] platform=%s keyword=%s page=%s saved=%s", self.platform, keyword, page, collected)
                try:
                    result = await fetch_checked(lambda: self.adapter.fetch(keyword, page, cursor), self.options,
                                                 f"{self.platform} keyword={keyword} page={page}")
                except Exception as exc:
                    self.state.fail("search", f"{scope}:{keyword}:{page}", exc)
                    raise
                fresh = []
                observed_at = int(time.time() * 1000)
                for context in result.contexts:
                    key = self.adapter.key(context)
                    if key and key not in seen:
                        seen.add(key)
                        context["observed_at"] = observed_at
                        fresh.append(context)
                if maximum:
                    fresh = fresh[:maximum - collected]
                for context in fresh:
                    self.state.hit(self.adapter.key(context), keyword)
                    content = await self.process(context)
                    if content is not None:
                        collected += 1
                        await self.snapshot()
                        await self.comments(content)
                    await self.snapshot()
                repeated_page = bool(result.contexts) and not fresh
                completed = repeated_page or (not fresh and not result.continue_empty) or result.finished or bool(result.cursor and result.cursor == cursor) or bool(maximum and collected >= maximum)
                if completed:
                    reason = result.finish_reason or ("平台明确返回无更多结果" if result.finished else "无新内容或游标重复")
                    if maximum and collected >= maximum:
                        reason = "达到配置数量"
                    utils.logger.info("[CollectionSearch] platform=%s keyword=%s 翻页结束：%s", self.platform, keyword, reason)
                self.state.resolve("search", f"{scope}:{keyword}:{page}")
                page += 1
                cursor = result.cursor
                self.state.checkpoint_page(scope, keyword, page, collected, completed, cursor)
                if completed:
                    break
        utils.logger.info("[CollectionSearch] platform=%s 结束：%s", self.platform, self.stats)
