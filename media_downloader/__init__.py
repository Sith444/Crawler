# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

"""统一媒体下载器：流式传输、断点续传、指数退避重试、大小校验、DASH 合流。

平台特有的"下载哪个 URL"由 ``media_platform/<platform>/media.py`` 负责，
本包只处理"怎么把字节落到磁盘"，因此不认识任何平台。
"""

from .downloader import MediaDownloader
from .ffmpeg import ffmpeg_path, is_available as is_ffmpeg_available, merge_audio_video
from .types import (
    MediaDownloadError,
    MediaFatalError,
    MediaItem,
    MediaRetryableError,
    MediaType,
)

__all__ = [
    "MediaDownloader",
    "MediaDownloadError",
    "MediaFatalError",
    "MediaItem",
    "MediaRetryableError",
    "MediaType",
    "ffmpeg_path",
    "is_ffmpeg_available",
    "merge_audio_video",
]
