"""道路交通相关性识别、互动评分及独立筛选快照。"""

from contextlib import closing
from datetime import datetime
import html
import json
import logging
import math
from pathlib import Path
import re
import random
import sqlite3

from tools.platform_specs import PLATFORMS, identity


ROAD = re.compile(r"高速|高架|国道|省道|县道|公路|道路|路口|路段|隧道|匝道|收费站|立交|[GS]\d{1,4}")
PLACE = re.compile(r"[\u4e00-\u9fff]{2,8}(?:市|县|区|镇)")
VEHICLE = re.compile(r"车辆|[两三四五六七八九十多几\d]+辆车|机动车|轿车|货车|客车|大巴|公交车|网约车|出租车|摩托|机车|电摩|电动车|电动自行车|非机动车|渣土车|半挂车|汽车|驾驶员|驾驶人|司机|乘客|行人|骑手")
EVENT = re.compile(r"交通事故|车祸|追尾|相撞|碰撞|撞车|被撞|撞人|撞上|摔车|侧翻|车辆失控|肇事|逃逸|酒驾|醉驾|疲劳驾驶|超速|逆行|闯红灯|抢方向盘|拥堵|堵车|封路|交通管制|道路封闭|绕行|道路施工|不礼让|礼让行人|随意变道|恶意别车")
OFFICIAL = re.compile(r"交警|路政|交管|公安|警方|通报|应急|消防|处置")
ANCHOR = re.compile(r"交通事故|车祸|追尾|侧翻|酒驾|醉驾|疲劳驾驶|抢方向盘|交通管制|封路|拥堵|堵车|道路施工|交警|路政")
TOPIC = re.compile(r"交通安全|安全出行|道路安全|行车安全|驾驶安全|安全驾驶|安全行车|文明行车|文明骑行|交通规则|交通法规|交通秩序|交通警示|交通出行|出行提示|路况|驾考|科目一|科目四|驾驶技巧|防御性驾驶|事故责任|事故认定|事故理赔|交通事故赔偿|道路交通|智慧交通|交通科普|交通设施|交通信号|信号灯|应急车道|非机动车过街")
VIRTUAL = re.compile(r"都市天际线|城市天际线|模拟驾驶|驾驶模拟|游戏画面|游戏实况")
QUIZ = re.compile(r"成语填空|互动答题|形容.{0,10}成语|每日一学.*填空")


def clean_text(value):
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_count(value):
    """解析整数、小数及万/亿单位；缺失或异常值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("，", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿]?)\+?", text)
    if not match:
        return None
    return int(float(match[1]) * {"": 1, "万": 10000, "亿": 100000000}[match[2]])


def evaluate_text(text, min_length=5, min_dimensions=1):
    text = text.replace("交通大学", "大学")
    dimensions = sum((bool(ROAD.search(text) or PLACE.search(text)),
                      bool(VEHICLE.search(text)), bool(EVENT.search(text) or TOPIC.search(text)),
                      bool(OFFICIAL.search(text))))
    if len(text) < min_length:
        return False, "正文过短", dimensions
    if QUIZ.search(text):
        return False, "答题内容", dimensions
    if VIRTUAL.search(text):
        return False, "模拟或游戏内容", dimensions
    if not (ANCHOR.search(text) or TOPIC.search(text)
            or (VEHICLE.search(text) and (EVENT.search(text) or ROAD.search(text)))
            or (ROAD.search(text) and OFFICIAL.search(text))):
        return False, "缺少道路交通语境", dimensions
    if dimensions < min_dimensions:
        return False, "交通描述维度不足", dimensions
    return True, "交通相关", dimensions


def timestamp_seconds(value):
    """将秒或毫秒 Unix 时间戳统一为秒；缺失、非正数或非有限值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    value = value / 1000 if value >= 100_000_000_000 else value
    return value if value <= 253402300799 else None


def compute_heat(row, weights=(0.1365, 0.2385, 0.6250), rho=0.75, gravity=1.8):
    """用发布至互动量采集的天数计算网络传播力，不使用筛选运行时间。"""
    published = timestamp_seconds(row.get("create_time"))
    observed = timestamp_seconds(row.get("last_modify_ts"))
    age = (observed - published) / 86400 if published is not None and observed is not None else None
    status = "完整"
    if age is None:
        status = "发布时间或互动量采集时间缺失或异常"
    elif age < 0:
        status = "互动量采集时间早于发布时间"
    counts = [parse_count(row.get(key)) for key in ("liked_count", "comments_count", "shared_count")]
    if any(n is None for n in counts):
        status = "互动量缺失或格式异常" if status == "完整" else status + "；互动量缺失或格式异常"
    score = rho * sum(w * n for w, n in zip(weights, counts)) / (age + 2) ** gravity if status == "完整" else None
    return {"heat_score": score, "heat_status": status, "heat_age_days": age,
            "heat_observed_at": observed, "heat_rank": None}


def platform_heat(row, spec, weights, rho, gravity):
    if spec.heat_method in ("paper", "paper_adapted"):
        normalized = dict(row, create_time=row.get(spec.published))
        normalized.update(zip(("liked_count", "comments_count", "shared_count"), (row.get(key) for key in spec.metrics)))
        return compute_heat(normalized, weights, rho, gravity)
    values = [parse_count(row.get(key)) for key in spec.metrics]
    valid = all(value is not None for value in values)
    return {"heat_score": sum(values) if valid else None,
            "heat_status": "可用互动量合计，未作时间衰减" if valid else "互动量缺失或格式异常",
            "heat_observed_at": timestamp_seconds(row.get("last_modify_ts")),
            "heat_age_days": None, "heat_rank": None}


def filter_database(db_path, output_root, *, platform="wb", min_length=5, min_dimensions=1,
                    weights=(0.1365, 0.2385, 0.6250), rho=0.75, gravity=1.8,
                    min_heat=None, top_ratio=0.2,
                    export_excel=True, export_jsonl=False, review_per_group=50):
    """只读平台原库，将相关帖子、热点标记、关联评论和筛选记录写入独立数据库。"""
    spec = PLATFORMS[platform]
    if (not 0 < top_ratio <= 1 or not 1 <= min_dimensions <= 4 or min_length < 0
            or (min_heat is not None and (not math.isfinite(min_heat) or min_heat < 0))
            or not math.isfinite(rho) or rho <= 0 or not math.isfinite(gravity) or gravity <= 0
            or not isinstance(review_per_group, int) or review_per_group < 0):
        raise ValueError("Invalid filtering thresholds")
    if len(weights) != 3 or any(not math.isfinite(w) or w < 0 for w in weights) or not any(weights):
        raise ValueError("Heat weights must contain three finite nonnegative values")
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    related, audit, comments = [], [], []
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        schemas = [source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                   for table in (spec.posts, spec.comments)]
        if not all(schemas):
            raise ValueError(f"Post/comment tables are missing for {platform}")
        seen = set()
        for record in source.execute(f'SELECT * FROM "{spec.posts}" ORDER BY last_modify_ts DESC, id DESC'):
            row = dict(record)
            note_id = identity(row, spec.keys)
            text = clean_text(" ".join(dict.fromkeys(str(row.get(key) or "") for key in spec.texts)))
            if not note_id:
                keep, reason, dimensions = False, "缺少帖子ID", 0
            elif note_id in seen:
                keep, reason, dimensions = False, "重复帖子ID", 0
            else:
                seen.add(note_id)
                keep, reason, dimensions = evaluate_text(text, min_length, min_dimensions)
            heat = platform_heat(row, spec, weights, rho, gravity)
            audit.append((row["id"], note_id, text, int(keep), reason, dimensions,
                          heat["heat_score"], heat["heat_status"], heat["heat_age_days"], heat["heat_observed_at"]))
            if keep:
                if "content" in row:
                    row["content"] = text
                row.update(traffic_text=text, heat_model=spec.heat_method, traffic_dimensions=dimensions, is_hot=0, **heat)
                related.append(row)
        related.sort(key=lambda row: (-(row["heat_score"] if row["heat_score"] is not None else -1), identity(row, spec.keys)))
        eligible = [row for row in related if row["heat_score"] is not None and row["heat_score"] > 0]
        top_n = math.ceil(len(eligible) * top_ratio)
        for rank, row in enumerate(eligible, 1):
            row["heat_rank"] = rank
            row["is_hot"] = int(rank <= top_n and (min_heat is None or row["heat_score"] >= min_heat))
        kept_ids = {identity(row, spec.keys) for row in related}
        seen_comments = set()
        for record in source.execute(f'SELECT * FROM "{spec.comments}" ORDER BY last_modify_ts DESC, id DESC'):
            row = dict(record)
            comment_id = str(row.get("comment_id") or "").strip()
            if identity(row, spec.keys) not in kept_ids or not comment_id or comment_id in seen_comments:
                continue
            seen_comments.add(comment_id)
            row["content"] = clean_text(row.get("content"))
            comments.append(row)

    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    batch = Path(output_root) / platform / "filtered" / stamp
    batch.mkdir(parents=True, exist_ok=False)
    output_db = batch / "traffic_filtered.db"
    hot_source_ids = {row["id"] for row in related if row["is_hot"]}
    groups = {
        "热点候选": [row for row in audit if row[3] and row[0] in hot_source_ids],
        "相关非热点": [row for row in audit if row[3] and row[0] not in hot_source_ids],
        "相关性剔除": [row for row in audit if not row[3] and row[4] not in ("缺少帖子ID", "重复帖子ID")],
    }
    rng = random.Random(0)
    reviews = [(group, row[0], row[1], row[2], row[4], row[6])
               for group, rows in groups.items() for row in rng.sample(rows, min(review_per_group, len(rows)))]
    summary = {
        "原始帖子数": len(audit), "交通相关帖子数": len(related),
        "热点帖子数": sum(row["is_hot"] for row in related), "相关评论数": len(comments),
        "热度异常帖子数": sum(row["heat_score"] is None for row in related),
        "有效非零热度帖子数": len(eligible),
        "平台": platform, "热度模型": spec.heat_method, "使用的互动字段": list(spec.metrics),
        "数据范围": "平台累计数据快照；帖子数量不代表独立事件数量；分数不跨平台比较",
        "热度方法": {"paper": "论文公式7的网络传播力指标，不是完整模型或官方热度",
                       "paper_adapted": "微博网络传播力公式的跨平台字段适配，需另行验证",
                       "native_total": "实际可用互动量合计，不套用不完整的微博公式"}[spec.heat_method],
        "公式": "rho*(点赞权重*L+评论权重*C+转发权重*R)/(发布至互动量采集天数+2)^gravity" if spec.heat_method != "native_total" else "+".join(spec.metrics),
        "文献": "黄微等. 网络舆情推文的热度测度模型构建. 图书情报工作,2019,63(20):17-25. DOI:10.13266/j.issn.0252-3116.2019.20.002",
        "比例系数": rho, "时间衰减指数": gravity,
        "点赞评论转发权重": list(weights), "最低分数": min_heat, "前列比例": top_ratio,
        "最短正文长度": min_length, "最少维度数": min_dimensions,
        "相关性规则": "保留道路交通事件、交通安全科普、驾驶知识及出行提示；不强制包含具体事件；排除答题和模拟游戏内容",
        "排序规则": "有效非零热度降序、帖子ID升序，取前ceil(有效非零帖子数×比例)作为候选；可另设最低分；异常和零分不入候选",
        "时间口径": (f"{spec.published}为发布时间，last_modify_ts为互动量采集时间；自动统一秒/毫秒，按天衰减" if spec.heat_method != "native_total" else "累计可用互动量，不作时间衰减"),
        "抽检样本数": len(reviews), "每组抽检上限": review_per_group,
        "抽检说明": "按热点、相关非热点、相关性剔除分组随机抽样；人工标签留空，不代表已验证准确率；重复及缺ID记录不进入语义抽检",
    }
    with closing(sqlite3.connect(output_db)) as dest:
        for schema in schemas:
            dest.execute(schema[0])
        for name, kind in (("heat_score", "REAL"), ("traffic_dimensions", "INTEGER"),
                           ("is_hot", "INTEGER"), ("heat_status", "TEXT"), ("heat_age_days", "REAL"),
                           ("heat_observed_at", "REAL"), ("heat_rank", "INTEGER"), ("traffic_text", "TEXT"), ("heat_model", "TEXT")):
            dest.execute(f'ALTER TABLE "{spec.posts}" ADD COLUMN "{name}" {kind}')
        for table, rows in ((spec.posts, related), (spec.comments, comments)):
            if rows:
                columns = list(rows[0])
                fields = ",".join(f'"{column}"' for column in columns)
                placeholders = ",".join("?" for _ in columns)
                dest.executemany(f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})',
                                 [[row[column] for column in columns] for row in rows])
        dest.execute("CREATE TABLE traffic_screening (id INTEGER PRIMARY KEY, source_id INTEGER, note_id TEXT, content TEXT, kept INTEGER, reason TEXT, dimensions INTEGER, heat_score REAL, heat_status TEXT, heat_age_days REAL, heat_observed_at REAL)")
        dest.executemany("INSERT INTO traffic_screening (source_id,note_id,content,kept,reason,dimensions,heat_score,heat_status,heat_age_days,heat_observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", audit)
        dest.execute(f'CREATE VIEW traffic_hot_note AS SELECT * FROM "{spec.posts}" WHERE is_hot=1')
        join_keys = " AND ".join(f'c."{key}"=p."{key}"' for key in spec.keys)
        dest.execute(f'CREATE VIEW traffic_hot_comment AS SELECT c.* FROM "{spec.comments}" c WHERE EXISTS (SELECT 1 FROM traffic_hot_note p WHERE {join_keys})')
        dest.execute("CREATE TABLE traffic_summary (id INTEGER PRIMARY KEY, item TEXT, value TEXT)")
        dest.execute("CREATE TABLE traffic_review (id INTEGER PRIMARY KEY, sample_group TEXT, source_id INTEGER, note_id TEXT, content TEXT, system_reason TEXT, heat_score REAL, human_related INTEGER, human_hot INTEGER, review_notes TEXT)")
        dest.executemany("INSERT INTO traffic_review (sample_group,source_id,note_id,content,system_reason,heat_score) VALUES (?,?,?,?,?,?)", reviews)
        dest.executemany("INSERT INTO traffic_summary (item,value) VALUES (?,?)",
                         [(key, json.dumps(value, ensure_ascii=False)) for key, value in summary.items()])
        dest.commit()
    logging.getLogger("MediaCrawler").info("[TrafficFilter] %s database=%s", summary, output_db)
    from tools.sqlite_exporter import export_sqlite_excel, export_sqlite_jsonl

    if export_excel:
        export_sqlite_excel(output_db, platform, output_root, output_dir=batch,
                            extra_tables=(("热点帖子", "traffic_hot_note"), ("热点评论", "traffic_hot_comment"),
                                          ("筛选记录", "traffic_screening"), ("筛选参数", "traffic_summary"),
                                          ("人工抽检", "traffic_review")),
                            scope="累计交通相关帖及关联评论；候选在平台内排序，公式和字段见筛选参数表")
    if export_jsonl:
        export_sqlite_jsonl(output_db, platform, output_root, output_dir=batch / "jsonl")
    return output_db, summary


def filter_from_config():
    """读取当前平台的数据字段及筛选参数。"""
    import config

    if config.PLATFORM not in PLATFORMS:
        logging.getLogger("MediaCrawler").info("[TrafficFilter] platform=%s is not supported", config.PLATFORM)
        return None
    root = Path(__file__).resolve().parent.parent
    return filter_database(
        config.SQLITE_DB_PATH, config.SAVE_DATA_PATH or root / "data",
        platform=config.PLATFORM,
        min_length=config.TRAFFIC_MIN_CONTENT_LEN, min_dimensions=config.TRAFFIC_MIN_EVENT_DIMENSIONS,
        weights=config.TRAFFIC_HEAT_WEIGHTS, min_heat=config.TRAFFIC_MIN_HEAT_SCORE,
        rho=config.TRAFFIC_HEAT_RHO, gravity=config.TRAFFIC_HEAT_GRAVITY,
        top_ratio=config.TRAFFIC_HOT_TOP_RATIO, export_excel=config.SQLITE_EXPORT_EXCEL,
        export_jsonl=config.SQLITE_EXPORT_JSONL,
        review_per_group=config.TRAFFIC_REVIEW_SAMPLE_PER_GROUP,
    )
