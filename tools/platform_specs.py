"""平台数据表、正文标识、评论关联及互动指标映射。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    posts: str
    comments: str
    keys: tuple
    texts: tuple
    published: str
    metrics: tuple
    client: str
    heat_method: str


PLATFORMS = {
    "wb": PlatformSpec("weibo_note", "weibo_note_comment", ("note_id",), ("content",), "create_time", ("liked_count", "comments_count", "shared_count"), "wb_client", "paper"),
    "bili": PlatformSpec("bilibili_video", "bilibili_video_comment", ("video_id",), ("title", "desc"), "create_time", ("liked_count", "video_comment", "video_share_count"), "bili_client", "paper_adapted"),
    "xhs": PlatformSpec("xhs_note", "xhs_note_comment", ("note_id",), ("title", "desc"), "time", ("liked_count", "comment_count", "share_count"), "xhs_client", "paper_adapted"),
    "dy": PlatformSpec("douyin_aweme", "douyin_aweme_comment", ("aweme_id",), ("title", "desc"), "create_time", ("liked_count", "comment_count", "share_count"), "dy_client", "paper_adapted"),
    "ks": PlatformSpec("kuaishou_video", "kuaishou_video_comment", ("video_id",), ("title", "desc"), "create_time", ("liked_count",), "ks_client", "native_total"),
    "tieba": PlatformSpec("tieba_note", "tieba_comment", ("note_id",), ("title", "desc"), "publish_time", ("total_replay_num",), "tieba_client", "native_total"),
    "zhihu": PlatformSpec("zhihu_content", "zhihu_comment", ("content_id", "content_type"), ("title", "content_text", "desc"), "created_time", ("voteup_count", "comment_count"), "zhihu_client", "native_total"),
}


def identity(row, keys):
    values = tuple(str(row.get(key) or "").strip() for key in keys)
    if not all(values):
        return ""
    return values[0] if len(values) == 1 else ":".join(values)


def collection_options(settings, platform):
    """将公共参数与平台覆盖项合并，返回当前平台使用的设置。"""
    options = {
        "interval": getattr(settings, "CRAWLER_MAX_SLEEP_SEC", 0.5),
        "attempts": getattr(settings, "COLLECTION_REQUEST_ATTEMPTS", 3),
        "backoff": getattr(settings, "COLLECTION_RETRY_BACKOFF_SEC", 2),
        "max_wait": getattr(settings, "COLLECTION_MAX_RETRY_WAIT_SEC", 60),
        "resume": getattr(settings, "COLLECTION_RESUME_SEARCH", True),
        "detail_hours": getattr(settings, "COLLECTION_DETAIL_REFRESH_HOURS", 24),
        "comment_hours": getattr(settings, "COLLECTION_COMMENT_REFRESH_HOURS", 24),
        "staged_comments": getattr(settings, "COLLECTION_STAGED_COMMENTS", True),
        "snapshot_posts": getattr(settings, "COLLECTION_SNAPSHOT_EVERY_POSTS", 200),
        "snapshot_seconds": getattr(settings, "COLLECTION_SNAPSHOT_EVERY_SECONDS", 600),
    }
    options.update(getattr(settings, "PLATFORM_COLLECTION_OPTIONS", {}).get(platform, {}))
    return options
