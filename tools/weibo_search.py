"""微博搜索编排：断点续采、全文缓存、评论补采及周期快照。"""

import asyncio
from pathlib import Path
import time

from tools.traffic_filter import ANCHOR, EVENT, QUIZ, ROAD, VEHICLE, clean_text, evaluate_text
from tools.weibo_state import WeiboState
from tools.platform_specs import collection_options
from tools.search_paging import SearchPage, SearchResponseError, fetch_checked
from tools.weibo_requests import WeiboPaused
from tools import utils


def needs_comments(text, min_length=15, min_dimensions=2):
    """保留相关及不确定样本；明显无关内容暂缓采集评论。"""
    text = clean_text(text)
    if evaluate_text(text, min_length, min_dimensions)[0]:
        return True
    if QUIZ.search(text):
        return False
    return len(text) < min_length or bool(ANCHOR.search(text) or EVENT.search(text) or ROAD.search(text) or VEHICLE.search(text))


class WeiboSearch:
    def __init__(self, crawler, settings, store, keyword_context, card_filter, search_types):
        self.crawler, self.config, self.store = crawler, settings, store
        self.options = collection_options(settings, "wb")
        self.keyword_context, self.card_filter, self.search_types = keyword_context, card_filter, search_types
        root = Path(settings.SAVE_DATA_PATH) if settings.SAVE_DATA_PATH else Path(__file__).resolve().parent.parent / "data"
        self.state = WeiboState(root / "wb" / "collection_state.db")
        self.attempted_details, self.attempted_comments = set(), set()
        self.snapshot_time = time.monotonic()
        self.snapshot_posts = 0
        self.stats = dict(saved_posts=0, detail_requests=0, detail_cache_hits=0, comment_requests=0,
                          comment_skips=0, failures=0)

    async def enrich(self, item):
        blog = item.get("mblog") or {}
        note_id = str(blog.get("id") or "")
        if not note_id or not self.config.ENABLE_WEIBO_FULL_TEXT:
            return item
        cached = self.state.cached_detail(note_id, self.options["detail_hours"])
        if cached is not None:
            blog["text"] = cached
            self.stats["detail_cache_hits"] += 1
            return item
        if not blog.get("isLongText"):
            self.state.save_detail(note_id, clean_text(blog.get("text")))
            return item
        if note_id in self.attempted_details:
            return item
        self.attempted_details.add(note_id)
        self.stats["detail_requests"] += 1
        try:
            full = await self.crawler.wb_client.get_note_info_by_id(note_id)
            if not full or not full.get("mblog") or not str(full["mblog"].get("text") or "").strip():
                raise ValueError("全文接口未返回帖子")
            # 正文取详情页，互动量保留本次搜索响应，避免缓存覆盖新互动量。
            blog["text"] = full["mblog"].get("text") or blog.get("text", "")
            self.state.save_detail(note_id, clean_text(blog["text"]))
        except Exception as exc:
            self.state.fail("detail", note_id, exc)
            self.stats["failures"] += 1
            utils.logger.warning("[WeiboSearch] 全文待补采 note_id=%s: %s", note_id, exc)
        return item

    async def comments(self, note_id, text):
        if not self.config.ENABLE_GET_COMMENTS:
            return
        note_id = str(note_id)
        limit = self.config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES
        if (note_id in self.attempted_comments or
                self.state.comments_fresh(note_id, self.options["comment_hours"], limit) or
                (self.options["staged_comments"] and not needs_comments(
                    text, self.config.TRAFFIC_MIN_CONTENT_LEN, self.config.TRAFFIC_MIN_EVENT_DIMENSIONS))):
            self.stats["comment_skips"] += 1
            return
        self.attempted_comments.add(note_id)
        self.stats["comment_requests"] += 1
        try:
            await self.crawler.wb_client.get_note_all_comments(
                note_id=note_id, crawl_interval=0,
                callback=self.store.batch_update_weibo_note_comments, max_count=limit,
            )
            self.state.save_comments(note_id, limit)
        except Exception as exc:
            self.state.fail("comments", note_id, exc)
            self.stats["failures"] += 1
            utils.logger.warning("[WeiboSearch] 评论待补采 note_id=%s: %s", note_id, exc)

    async def retry_failed(self):
        ids = list(dict.fromkeys(self.state.failed("detail") + self.state.failed("comments")))
        for note_id in ids:
            try:
                self.keyword_context.set(self.state.keyword(note_id))
                item = await self.crawler.wb_client.get_note_info_by_id(note_id)
                blog = item.get("mblog") or {}
                if not blog.get("id") or not blog.get("text"):
                    raise ValueError("补采接口未返回完整帖子")
                await self.store.update_weibo_note(item)
                self.state.save_detail(note_id, clean_text(blog["text"]))
                await self.comments(note_id, blog["text"])
                await self.maybe_snapshot()
            except Exception as exc:
                self.state.fail("detail", note_id, exc)
                self.stats["failures"] += 1

    async def maybe_snapshot(self):
        if self.config.SAVE_DATA_OPTION != "sqlite":
            return
        if not (self.config.SQLITE_EXPORT_EXCEL or self.config.SQLITE_EXPORT_JSONL):
            return
        posts_due = self.options["snapshot_posts"] > 0 and self.snapshot_posts >= self.options["snapshot_posts"]
        time_due = self.options["snapshot_seconds"] > 0 and time.monotonic() - self.snapshot_time >= self.options["snapshot_seconds"]
        if not (posts_due or time_due):
            return
        from tools.sqlite_exporter import export_from_config

        try:
            await asyncio.to_thread(export_from_config)
            self.state.resolve("snapshot", "export")
        except Exception as exc:
            self.state.fail("snapshot", "export", exc)
            utils.logger.warning("[WeiboSearch] 周期导出失败，SQLite 数据保留：%s", exc)
        self.snapshot_posts = 0
        self.snapshot_time = time.monotonic()
        utils.logger.info("[WeiboSearch] 本轮处理统计：%s", self.stats)

    async def run(self):
        try:
            max_notes = self.config.CRAWLER_MAX_NOTES_COUNT
            if max_notes < 0:
                raise ValueError("CRAWLER_MAX_NOTES_COUNT must be nonnegative")
            search_type = getattr(self.search_types, self.config.WEIBO_SEARCH_TYPE.upper())
            keywords = list(dict.fromkeys(word.strip() for word in self.config.KEYWORDS.split(",") if word.strip()))
            scope = self.state.begin(keywords, self.config.WEIBO_SEARCH_TYPE, self.config.START_PAGE,
                                     max_notes, self.options["resume"])
            for keyword in keywords:
                self.keyword_context.set(keyword)
                page, collected, completed = self.state.progress(scope, keyword)
                if completed:
                    continue
                seen_ids = set()
                while max_notes == 0 or collected < max_notes:
                    utils.logger.info("[WeiboSearch] keyword=%s page=%s saved=%s", keyword, page, collected)
                    try:
                        result = await fetch_checked(lambda: self._fetch_page(keyword, page, search_type),
                                                     self.options, f"wb keyword={keyword} page={page}", paused_type=WeiboPaused)
                    except Exception as exc:
                        self.state.fail("search", f"{scope}:{keyword}:{page}", exc)
                        raise
                    rows = result.contexts
                    observed_at = int(time.time() * 1000)
                    fresh = []
                    for item in rows:
                        note_id = str((item.get("mblog") or {}).get("id") or "")
                        if note_id and note_id not in seen_ids:
                            item["_observed_at"] = observed_at
                            seen_ids.add(note_id)
                            fresh.append(item)
                    if not fresh and (rows or not result.continue_empty or result.finished):
                        self.state.checkpoint(scope, keyword, page, collected, True)
                        utils.logger.info("[WeiboSearch] keyword=%s 翻页结束：%s", keyword, result.finish_reason or "无更多结果或页面重复")
                        break
                    if max_notes:
                        fresh = fresh[:max_notes - collected]
                    page_comments = []
                    for item in fresh:
                        item = await self.enrich(item)
                        blog = item["mblog"]
                        await self.store.update_weibo_note(item)
                        self.state.hit(blog["id"], keyword)
                        await self.crawler.download_media(blog)
                        page_comments.append((blog["id"], blog.get("text", "")))
                        collected += 1
                        self.snapshot_posts += 1
                        self.stats["saved_posts"] += 1
                        await self.maybe_snapshot()
                    for note_id, text in page_comments:
                        await self.comments(note_id, text)
                        await self.maybe_snapshot()
                    self.state.resolve("search", f"{scope}:{keyword}:{page}")
                    page += 1
                    self.state.checkpoint(scope, keyword, page, collected, bool(max_notes and collected >= max_notes))
            await self.retry_failed()
            utils.logger.info("[WeiboSearch] 搜索结束，处理统计：%s", self.stats)
        finally:
            self.state.close()

    async def _fetch_page(self, keyword, page, search_type):
        result = await self.crawler.wb_client.get_note_by_keyword(keyword=keyword, page=page, search_type=search_type)
        if not isinstance(result, dict) or not isinstance(result.get("cards"), list):
            raise SearchResponseError("微博搜索未返回正常的卡片列表")
        cards = result["cards"]
        confirmed = result.get("_empty_confirmed") is True
        return SearchPage(self.card_filter(cards), {}, finished=confirmed,
                          continue_empty=bool(cards), unconfirmed_empty=not cards and not confirmed)
