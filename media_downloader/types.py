# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

"""媒体下载的数据结构与异常定义。

本模块只描述"要下载什么"，不包含任何平台细节与 IO 逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple


class MediaType(str, Enum):
    """媒体类型"""

    IMAGE = "image"
    VIDEO = "video"


class MediaDownloadError(Exception):
    """媒体下载失败（基类）"""


class MediaRetryableError(MediaDownloadError):
    """可重试的下载失败：网络抖动、5xx、429、响应体不完整等"""


class MediaFatalError(MediaDownloadError):
    """不可重试的下载失败：4xx（403 多为签名过期、404 为资源不存在）"""


@dataclass(slots=True)
class MediaItem:
    """一个待下载的媒体资源。

    下载器只认识这个结构，平台差异全部通过字段注入：

    :param url: 主下载地址
    :param media_type: 媒体类型（图片/视频）
    :param content_id: 所属帖子 ID，决定落盘目录
    :param stem: 文件名主干（如 001 / cover / video），扩展名由下载器推断
    :param extension: 显式扩展名（含点）。为空时依次从 URL 后缀、Content-Type 推断
    :param backup_urls: 主地址重试耗尽后依次尝试的备用地址（多 CDN 场景）
    :param audio_url: 非空表示 DASH 音视频分轨资源，需要 ffmpeg 合流
    :param audio_backup_urls: 音轨的备用地址
    :param headers: 针对该资源的请求头覆盖项（如 B 站 Cookie）
    """

    url: str
    media_type: MediaType
    content_id: str
    stem: str = "media"
    extension: Optional[str] = None
    backup_urls: Tuple[str, ...] = ()
    audio_url: Optional[str] = None
    audio_backup_urls: Tuple[str, ...] = ()
    headers: Optional[Mapping[str, str]] = None

    @property
    def is_dash(self) -> bool:
        """是否为 DASH 音视频分轨资源（需要下载两路流后合流）"""
        return bool(self.audio_url)
