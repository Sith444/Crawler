"""将 SQLite 中的平台累计数据导出为 Excel 或 JSONL 快照。"""

from contextlib import closing
from datetime import datetime
import logging
import json
from pathlib import Path
import sqlite3

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from tools.platform_specs import PLATFORMS


PLATFORM_TABLES = {key: (spec.posts, spec.comments) for key, spec in PLATFORMS.items()}


def export_sqlite_excel(db_path, platform, output_root, *, extra_tables=(), output_dir=None,
                        scope="该平台数据库中的累计数据快照，不代表本次新增数据"):
    """只读数据库，按平台导出帖子、评论和导出说明；返回文件路径。"""
    tables = PLATFORM_TABLES[platform]
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    workbook = Workbook(write_only=True)
    exported_at = datetime.now().astimezone()
    counts = []
    truncated = 0
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        # 在同一读取事务中导出帖子和评论，保持快照一致。
        for title, table in list(zip(("帖子", "评论"), tables)) + list(extra_tables):
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY id')
            sheet = workbook.create_sheet(title)
            sheet.freeze_panes = "A2"
            sheet.append([column[0] for column in cursor.description])
            count = 0
            for row in cursor:
                if count >= 1_048_575:
                    raise ValueError(f"{table} exceeds the Excel worksheet row limit")
                cells = []
                for value in row:
                    if isinstance(value, str):
                        value = ILLEGAL_CHARACTERS_RE.sub("", value)
                        truncated += int(len(value) > 32767)
                        cell = WriteOnlyCell(sheet, value=value[:32767])
                        # 帖子内容按文本写入，保留长 ID，避免被识别为公式。
                        cell.data_type = "s"
                        cells.append(cell)
                    else:
                        cells.append(value)
                sheet.append(cells)
                count += 1
            counts.append(count)

    info = workbook.create_sheet("导出说明")
    for row in [
        ("项目", "内容"),
        ("平台", platform),
        ("导出时间", exported_at.isoformat()),
        ("数据范围", scope),
        ("帖子条数", counts[0]),
        ("评论条数", counts[1]),
        ("超长文本截断单元格数", truncated),
        ("文本说明", "Excel 不支持的控制字符已移除；超长单元格保留前32767字符，完整数据保存在SQLite中"),
    ]:
        info.append(row)

    output_dir = Path(output_dir) if output_dir is not None else Path(output_root) / platform / "export"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = exported_at.strftime("%Y%m%d_%H%M%S_%f")
    output = output_dir / f"{platform}_snapshot_{stamp}.xlsx"
    workbook.save(output)
    logging.getLogger("MediaCrawler").info(
        "[ExcelExport] posts=%s comments=%s file=%s", *counts[:2], output
    )
    return output


def export_sqlite_jsonl(db_path, platform, output_root, *, output_dir=None):
    """在同一读取事务中，分别导出帖子和评论，每行保存一条完整记录。"""
    tables = PLATFORM_TABLES[platform]
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    output_dir = Path(output_dir) if output_dir is not None else Path(output_root) / platform / "export" / "jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    outputs = []
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        for kind, table in zip(("contents", "comments"), tables):
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY id')
            columns = [column[0] for column in cursor.description]
            output = output_dir / f"{platform}_snapshot_{stamp}_{kind}.jsonl"
            count = 0
            with output.open("x", encoding="utf-8") as stream:
                for row in cursor:
                    stream.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False) + "\n")
                    count += 1
            outputs.append(output)
            logging.getLogger("MediaCrawler").info(
                "[JsonlExport] scope=platform_snapshot type=%s rows=%s file=%s",
                kind, count, output,
            )
    return outputs


def export_from_config():
    """按独立开关导出当前平台的累计数据快照。"""
    import config

    root = Path(__file__).resolve().parent.parent
    output_root = Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else root / "data"
    outputs = []
    errors = []
    for enabled, exporter in (
        (config.SQLITE_EXPORT_EXCEL, export_sqlite_excel),
        (config.SQLITE_EXPORT_JSONL, export_sqlite_jsonl),
    ):
        if enabled:
            try:
                outputs.append(exporter(config.SQLITE_DB_PATH, config.PLATFORM, output_root))
            except Exception as exc:
                errors.append(f"{exporter.__name__}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return outputs
