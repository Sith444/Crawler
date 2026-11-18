# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# Weibo platform configuration

# Search type, the specific enumeration value is in media_platform/weibo/field.py
WEIBO_SEARCH_TYPE = "popular"  # 热门搜索

# detail 模式的目标帖子；未指定目标时保持为空。
WEIBO_SPECIFIED_ID_LIST = []

# creator 模式采集的交管、交警和路网账号名单。
WEIBO_CREATOR_ID_LIST = [
    "2501519087",  # 公安部交通管理局
    "3267315877",  # 中国路网
    "3427645762",  # 北京交警
    "1910577521",  # 湖南交警
    "2085699127",  # 西安交警
    "2573119002",  # 南京交警
    "6052733332",  # 黑龙江交警
    "5889046080",  # 四川交警
    "1878396095",  # 成都交警
    "2309234963",  # 石家庄交警
    "2901636020",  # 深圳交警路况播报
    "3175480090",  # 山东高速交警
    "2245589952",  # 陕西交通12122
    "1914390204",  # 湖南高速公路
    "5995635638",  # 青海路网
]

# Whether to enable the function of crawling the full text of Weibo. It is enabled by default.
# If turned on, it will increase the probability of being risk controlled, which is equivalent to a keyword search request that will traverse all posts and request the post details again.
ENABLE_WEIBO_FULL_TEXT = True
