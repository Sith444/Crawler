# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# bilili platform configuration

# Control the number of videos/posts crawled per day
MAX_NOTES_PER_DAY = 1

# Specify Bilibili video URL list (supports complete URL or BV number)
# Example:
# - BV number: "BV1d54y1g7db"
BILI_SPECIFIED_ID_LIST = []

# Specify the URL list of Bilibili creators (supports full URL or UID)
# Example:
# - UID: "20813884"
BILI_CREATOR_ID_LIST = []

# Specify time range
START_DAY = "2024-01-01"
END_DAY = "2024-01-01"

# Search mode
BILI_SEARCH_MODE = "normal"
BILI_SEARCH_ORDER = "click"  # click=最多播放，空字符串=综合，pubdate=最新，dm=最多弹幕，stow=最多收藏

# Video definition (qn) configuration, common values:
# 16=360p, 32=480p, 64=720p, 80=1080p, 112=1080p high bit rate, 116=1080p60, 120=4K
# Note: Higher definition requires account/video support
BILI_QN = 80

# Whether to crawl user information
CREATOR_MODE = True

# Start crawling user information page number
START_CONTACTS_PAGE = 1

# Maximum number of crawled comments for a single video/post
CRAWLER_MAX_CONTACTS_COUNT_SINGLENOTES = 100

# Maximum number of crawled dynamics for a single video/post
CRAWLER_MAX_DYNAMICS_COUNT_SINGLENOTES = 50
