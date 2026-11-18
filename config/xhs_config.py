# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# Xiaohongshu platform configuration

# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
SORT_TYPE = "popularity_descending"
XHS_COMMENT_MODE = "browser"  # browser=读取已登录网页加载的一级评论，api=接口请求

# Specify the note URL list, which must carry the xsec_token parameter
XHS_SPECIFIED_NOTE_URL_LIST = []

# Specify the creator URL list, which needs to carry xsec_token and xsec_source parameters.

XHS_CREATOR_ID_LIST = []
