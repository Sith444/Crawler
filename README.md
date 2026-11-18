# 交通舆情多平台采集与筛选

围绕交通关键词采集内容、互动指标和一级评论，使用SQLite保存原始数据，按平台导出Excel并生成交通相关内容及热点候选。

## 平台与功能

支持微博、抖音、小红书、B站、快手、贴吧和知乎的采集适配。本次数据成果主要来自微博、抖音、小红书和B站。

- 关键词搜索、内容详情和关联评论采集。
- 关键词页码或游标断点、近期详情缓存、失败任务记录。
- 网络异常有限重试，登录失效或验证时保存进度并暂停。
- SQLite持续保存，Excel周期导出，JSONL通过开关控制。
- 交通相关性筛选、平台内热度排名、筛选原因记录和抽检样本输出。

## 环境与运行

需要Python 3.11或以上版本、Node.js及Chrome。安装依赖：

```powershell
python -m pip install -r requirements.txt
```

在`config/base_config.py`中设置Chrome路径、调试端口和采集参数。当前使用CDP连接已有Chrome窗口，需先以对应端口启动浏览器并手动完成登录。例如：

```powershell
& "D:\Google\Chrome\Application\chrome.exe" --remote-debugging-port=64356 --user-data-dir="$PWD\browser_data\chrome_wb_user_data_dir"
python main.py --platform wb --type search --save_data_option sqlite
```

Chrome路径按本机实际安装位置填写。平台参数：`wb`微博、`dy`抖音、`xhs`小红书、`bili`B站、`ks`快手、`tieba`贴吧、`zhihu`知乎。停止时按一次`Ctrl+C`，等待程序保存和导出。

## 配置入口

| 配置 | 作用 |
|---|---|
| `KEYWORDS` | 交通检索关键词 |
| `CRAWLER_MAX_NOTES_COUNT` | 内容数量上限，0表示不设数量上限 |
| `PLATFORM_COLLECTION_OPTIONS` | 分平台请求间隔、缓存、重试与快照参数 |
| `SQLITE_EXPORT_EXCEL` | 导出Excel |
| `SQLITE_EXPORT_JSONL` | 导出JSONL，默认关闭 |
| `ENABLE_TRAFFIC_FILTER` | 生成独立筛选结果 |

当前微博采用热门搜索，抖音采用最多点赞，B站采用最多播放，小红书采用热度优先。微博请求间隔0.5秒、抖音10秒、小红书2秒、B站1秒。小红书默认读取已登录网页加载的评论。

## 文件结构

| 文件或目录 | 作用 |
|---|---|
| `main.py` | 程序启动入口 |
| `base/` | 爬虫、登录与存储的基础接口 |
| `cache/` | 内存与Redis缓存实现 |
| `cmd_arg/` | 命令行参数解析与运行选项 |
| `config/` | 采集参数与平台配置 |
| `constant/` | 平台常量与状态定义 |
| `database/` | 数据库连接、会话与数据表模型 |
| `docs/` | 多平台采集与交通数据筛选说明 |
| `libs/` | 平台请求签名与浏览器辅助脚本 |
| `media_downloader/` | 图片、视频等媒体下载模块 |
| `media_platform/` | 各平台搜索、内容详情与评论采集 |
| `model/` | 平台内容与评论的数据结构 |
| `proxy/` | 代理服务接入与IP管理 |
| `store/` | 内容与评论的存储适配 |
| `tests/` | 采集、分页、存储与筛选测试 |
| `tools/` | 断点续采、请求控制、数据筛选与导出工具 |
| `var.py` | 采集任务的上下文变量 |
| `requirements.txt`、`pyproject.toml`、`uv.lock` | 依赖声明与版本管理 |
| `data/` | 本地断点、导出、筛选及运行记录，不上传 |
| `browser_data/` | 本地浏览器会话，不上传 |

原始内容与评论保存在`database/sqlite_tables.db`。各平台的`collection_state.db`保存断点、缓存和失败记录。Excel是累计快照，不能将不同时间的文件直接相加。筛选结果独立保存，不删除原始数据。

## 筛选方法

正文至少5字、至少命中一个描述维度，并具有明确的道路交通相关信号。保留交通事件、交通安全科普、驾驶知识和出行提示，记录剔除原因。

微博、抖音、小红书和B站使用带时间衰减的传播力指标：

`H = 0.75 × (0.1365L + 0.2385C + 0.6250R) / (Δt + 2)^1.8`

L、C、R分别为点赞、评论、转发或分享数，Δt为发布至互动量采集的天数。各平台对有效且非零热度内容降序取前20%，数量向上取整，不设置500分固定阈值。快手、贴吧和知乎按各自可用互动指标排序。分数不直接跨平台比较，人工抽检标签默认留空。

详见[多平台采集说明](docs/多平台采集说明.md)、[微博采集说明](docs/微博采集说明.md)和[交通数据筛选](docs/交通数据筛选.md)。

## 当前限制

平台验证和访问限制会暂停采集，无法保证获取全部历史内容。小红书单篇评论超时仍可能导致整个任务暂停。多个进程共用SQLite存在写入锁冲突，浏览器会话也需分开配置，当前建议分时运行。相关性规则和跨平台热度适配仍需人工核验。

## 许可

Copyright (c) [2024] [relakkes@gmail.com]

本仓库遵循NON-COMMERCIAL LEARNING LICENSE 1.1，许可及版权声明见[LICENSE](LICENSE)。第三方组件的许可和署名随对应文件保留。
