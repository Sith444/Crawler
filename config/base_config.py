# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

# Basic configuration
PLATFORM = "dy"  # 当前采集抖音；其他平台：wb | xhs | ks | bili | tieba | zhihu

# 是否使用海外版小红书 (rednote.com)
# 开启后 API 走 webapi.rednote.com，cookie 域使用 .rednote.com
XHS_INTERNATIONAL = False

# 搜索模式使用交通相关关键词采集候选帖子。
# 关键词仅在 search 模式下生效，不限制 creator 模式的账号时间线。
KEYWORDS = (
    "交通,交通事故,车祸,追尾事故,连环追尾,多车相撞,高速事故,高速追尾,隧道事故,"
    "客车事故,货车事故,网约车事故,交警通报,酒驾,醉驾,毒驾,疲劳驾驶,"
    "超速,闯红灯,逆行,抢方向盘,车辆失控,侧翻,"
    "道路封闭,高速封路,交通管制,拥堵,路况,道路施工,"
    "交通安全,安全出行,道路安全,行车安全,驾驶安全,交通规则,"
    "交通事故现场,车祸瞬间,交通事故警示录,事故警示,事故分析,"
    "无接触事故,鬼探头,横穿马路,随意变道,分心驾驶,错踩油门,交通事故责任,"
    "高速路况,路况播报,交通安全预警"
)
LOGIN_TYPE = "qrcode"  # qrcode or phone or cookie
COOKIES = ""
CRAWLER_TYPE = (
    "search"  # creator=指定账号采集，search=关键词搜索，detail=指定帖子
)
# Whether to enable IP proxy
ENABLE_IP_PROXY = False

# Number of proxy IP pools
IP_PROXY_POOL_COUNT = 2

# Proxy IP provider name
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp | static

# Static proxy configuration (used when IP_PROXY_PROVIDER_NAME is set to "static")
# Format: "http://your_home_domain:port" or "http://user:password@your_home_domain:port"
STATIC_PROXY_URL = ""

# Setting to True will not open the browser (headless browser)
# Setting False will open a browser
# If Xiaohongshu keeps scanning the code to log in but fails, open the browser and manually pass the sliding verification code.
# If Douyin keeps prompting failure, open the browser and see if mobile phone number verification appears after scanning the QR code to log in. If it does, manually go through it and try again.
HEADLESS = False

# Whether to save login status
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) 配置 ====================
# 是否启用 CDP 模式 - 使用用户本地的 Chrome/Edge 浏览器进行爬取，具有更好的反检测能力
# 开启后，会自动检测并启动用户的 Chrome/Edge 浏览器，通过 CDP 协议进行控制
# 通过浏览器会话复用登录状态。
ENABLE_CDP_MODE = True

# CDP 调试端口，用于与浏览器通信
# 如果端口被占用，系统会自动尝试下一个可用端口
CDP_DEBUG_PORT = 64356  # 已有浏览器的远程调试端口需与此一致

# 自定义浏览器路径（可选）
# 如果为空，系统会自动检测 Chrome/Edge 的安装路径
# Windows 示例: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS 示例: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = r"D:\Google\Chrome\Application\chrome.exe"

# 是否在 CDP 模式下启用无头模式
# 注意：即使设置为 True，某些反检测功能在无头模式下可能无法正常工作
CDP_HEADLESS = False

# 浏览器启动超时时间（秒）
BROWSER_LAUNCH_TIMEOUT = 60

# 是否连接用户已打开的浏览器，而不是启动新的浏览器
# 开启后，程序会连接一个已经启用了远程调试的浏览器
# 用户需要在 Chrome 中开启远程调试：chrome://inspect/#remote-debugging
# 或者使用命令行参数启动 Chrome：--remote-debugging-port=9222
# 连接前需确认浏览器已启用对应调试端口。
CDP_CONNECT_EXISTING = True

# 程序结束时是否自动关闭浏览器
# 设置为 False 可以保持浏览器运行，方便调试
AUTO_CLOSE_BROWSER = False  # 结束采集后保留浏览器，便于检查登录状态

# Data saving type option configuration, supports: csv, db, json, jsonl, sqlite, excel, postgres. It is best to save to DB, with deduplication function.
SAVE_DATA_OPTION = "sqlite"  # 原始数据写入 SQLite 数据库

# SQLite 模式下，采集正常结束后导出该平台的累计数据快照。
SQLITE_EXPORT_EXCEL = True
SQLITE_EXPORT_JSONL = False  # True=导出帖子和评论 JSONL，False=关闭

# SQLite 数据采集结束后，生成分平台筛选数据库和导出文件。
ENABLE_TRAFFIC_FILTER = True
TRAFFIC_MIN_CONTENT_LEN = 5  # 保留包含明确信息的交通短标题
TRAFFIC_MIN_EVENT_DIMENSIONS = 1  # 地点路网、车辆人员、交通事件或主题、官方处置至少命中一类
TRAFFIC_HEAT_WEIGHTS = (0.1365, 0.2385, 0.6250)  # 点赞、评论、转发的网络传播力权重
TRAFFIC_HEAT_RHO = 0.75  # 网络传播力比例系数
TRAFFIC_HEAT_GRAVITY = 1.8  # 时间衰减指数，时间间隔按天计算
TRAFFIC_MIN_HEAT_SCORE = None  # None=不设最低分；设置数值后作为额外筛选条件
TRAFFIC_HOT_TOP_RATIO = 0.2  # 有效且非零分数的交通相关帖中，前20%作为热点候选
TRAFFIC_REVIEW_SAMPLE_PER_GROUP = 50  # 热点、相关非热点、剔除样本各组的抽检上限

# Data saving path, if not specified by default, it will be saved to the data folder.
SAVE_DATA_PATH = ""

# Browser file configuration cached by the user's browser
USER_DATA_DIR = "%s_user_data_dir"  # %s will be replaced by platform name

# The number of pages to start crawling starts from the first page by default
START_PAGE = 1

# Control the number of crawled videos/posts
CRAWLER_MAX_NOTES_COUNT = 0  # 关键词搜索：0=不设数量上限，直到无结果或页面重复

# Controlling the number of concurrent crawlers
MAX_CONCURRENCY_NUM = 1

# 是否启用媒体下载（封面、视频，以及图文帖的图片），默认关闭。
# 开启后媒体文件按 {SAVE_DATA_PATH 或 data}/{platform}/media/{内容ID}/ 目录聚合存放。
# 支持的平台：xhs / dy / ks / bili / wb（tieba、zhihu 的数据结构中没有媒体字段，不支持）。
# 命令行开关：--get_media
ENABLE_GET_MEDIA = False

# 是否下载实况照片（Live Photo）随附的视频，统一控制抖音和小红书。
# 仅在 ENABLE_GET_MEDIA 开启时生效；关闭后只下载静态图片，不影响普通视频。
# 视频与静态图配对保存为 001_live.mp4 / 001.jpg，不合成相册实况照片。
DOWNLOAD_LIVE_PHOTO = True

# Whether to enable comment crawling mode. Comment crawling is enabled by default.
ENABLE_GET_COMMENTS = True

# Control the number of crawled first-level comments (single video/post)
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 20

# Whether to enable the mode of crawling second-level comments. By default, crawling of second-level comments is not enabled.
ENABLE_GET_SUB_COMMENTS = False

# Crawl interval
CRAWLER_MAX_SLEEP_SEC = 0.5

# 各平台共用的请求、续采和快照设置。
COLLECTION_REQUEST_ATTEMPTS = 3
COLLECTION_RETRY_BACKOFF_SEC = 2
COLLECTION_MAX_RETRY_WAIT_SEC = 60
COLLECTION_RESUME_SEARCH = True
COLLECTION_DETAIL_REFRESH_HOURS = 24
COLLECTION_COMMENT_REFRESH_HOURS = 24
COLLECTION_STAGED_COMMENTS = True  # 优先采集交通相关和待确认帖的评论
COLLECTION_SNAPSHOT_EVERY_POSTS = 200
COLLECTION_SNAPSHOT_EVERY_SECONDS = 600

# 公共采集参数支持按平台覆盖；未列出的选项使用公共默认值。
PLATFORM_COLLECTION_OPTIONS = {
    "bili": {"interval": 1},
    "xhs": {"interval": 2},
    "dy": {"interval": 10},
    "ks": {"interval": 1},
    "tieba": {"interval": 1},
    "zhihu": {"interval": 1},
}

# 是否禁用 SSL 证书验证。仅在使用企业代理、Burp Suite、mitmproxy 等会注入自签名证书的中间人代理时设为 True。
# 警告：禁用 SSL 验证将使所有流量暴露于中间人攻击风险，请勿在生产环境中开启。
DISABLE_SSL_VERIFY = False

from .bilibili_config import *
from .xhs_config import *
from .dy_config import *
from .ks_config import *
from .weibo_config import *
from .tieba_config import *
from .zhihu_config import *
