"""验证交通筛选、评论关联和导出结果。"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from openpyxl import load_workbook

from tools.traffic_filter import clean_text, compute_heat, evaluate_text, filter_database, parse_count


class TrafficFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "source.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE weibo_note (id INTEGER PRIMARY KEY, note_id TEXT, content TEXT, liked_count TEXT, comments_count TEXT, shared_count TEXT, last_modify_ts INTEGER)")
            conn.execute("CREATE TABLE weibo_note_comment (id INTEGER PRIMARY KEY, comment_id TEXT, note_id TEXT, content TEXT, last_modify_ts INTEGER)")
            conn.execute("ALTER TABLE weibo_note ADD COLUMN create_time INTEGER")
            conn.executemany("INSERT INTO weibo_note (id,note_id,content,liked_count,comments_count,shared_count,last_modify_ts) VALUES (?,?,?,?,?,?,?)", [
                (1, "a", "高速公路发生两车追尾，交警正在现场处置，请司机绕行。", "1000", "20", "10", 1),
                (2, "a", "<p>高速公路发生两车追尾，交警已到现场处置。</p>", "1.2万", "20", "10", 2),
                (3, "b", "城区道路施工实行交通管制，交警提醒司机提前绕行。", "1000", "0", "0", 1),
                (4, "c", "高速路口发生交通事故，两辆货车相撞，交警已到场。", "1", "0", "0", 1),
                (5, "d", "城区道路发生车祸，司机受伤，交警正在调查处置。", None, "20", "0", 1),
                (6, "e", "今天娱乐节目非常精彩，演员演技翻车引发大量讨论。", "99999", "9", "9", 1),
                (7, "f", "成语填空：高速追尾交通事故，交警提醒大家参与答题。", "99999", "9", "9", 1),
                (8, "", "城区道路发生交通事故，车辆追尾，交警正在处置。", "99999", "9", "9", 1),
            ])
            conn.execute("UPDATE weibo_note SET last_modify_ts=1700086400000+last_modify_ts, create_time=1700000000")
            conn.executemany("INSERT INTO weibo_note_comment VALUES (?,?,?,?,?)", [
                (1, "ca", "a", "旧评论", 1), (2, "ca", "a", "<b>注意安全</b>", 2),
                (3, "ce", "e", "无关帖的评论", 1), (4, "cb", "b", "感谢提醒", 1),
            ])
            conn.commit()

    def run_filter(self, **kwargs):
        return filter_database(self.db, self.root / "out", **kwargs)

    def test_snapshot_dedup_scores_and_comments(self):
        before = hashlib.sha256(self.db.read_bytes()).digest()
        output, summary = self.run_filter()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(), before)
        self.assertEqual((summary["原始帖子数"], summary["交通相关帖子数"], summary["热点帖子数"]), (8, 4, 1))
        with closing(sqlite3.connect(output)) as conn:
            note = conn.execute("SELECT id,heat_score,heat_age_days,heat_rank FROM weibo_note WHERE note_id='a'").fetchone()
            self.assertEqual(note[0], 2)
            self.assertAlmostEqual(note[1], 0.75 * (0.1365 * 12000 + 0.2385 * 20 + 0.625 * 10) / (3 + 0.002 / 86400) ** 1.8)
            self.assertAlmostEqual(note[2], 1, places=6)
            self.assertEqual(note[3], 1)
            self.assertEqual(conn.execute("SELECT note_id FROM traffic_hot_note").fetchall(), [("a",)])
            self.assertEqual(conn.execute("SELECT content FROM traffic_hot_comment").fetchall(), [("注意安全",)])
            self.assertEqual(conn.execute("SELECT count(*) FROM weibo_note_comment").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT heat_score,is_hot FROM weibo_note WHERE note_id='d'").fetchone(), (None, 0))
            self.assertEqual(conn.execute("SELECT reason FROM traffic_screening WHERE source_id=1").fetchone()[0], "重复帖子ID")
            self.assertEqual(conn.execute("SELECT count(*) FROM traffic_review WHERE human_related IS NOT NULL OR human_hot IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM traffic_review WHERE source_id IN (1,8)").fetchone()[0], 0)
        self.assertFalse(list(output.parent.rglob("*.jsonl")))
        book = load_workbook(next(output.parent.glob("*.xlsx")))
        try:
            self.assertEqual(book["帖子"].max_row, 5)
            self.assertEqual(book["热点帖子"].max_row, 2)
            self.assertEqual(book["筛选记录"].max_row, 9)
            self.assertIn("筛选参数", book.sheetnames)
            self.assertIn("人工抽检", book.sheetnames)
        finally:
            book.close()

    def test_switches_and_minimum_heat(self):
        output, summary = self.run_filter(export_excel=False, export_jsonl=True, top_ratio=1, min_heat=10)
        self.assertEqual(summary["热点帖子数"], 2)
        self.assertFalse(list(output.parent.glob("*.xlsx")))
        posts = next(output.parent.rglob("*_contents.jsonl"))
        rows = [json.loads(line) for line in posts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row["is_hot"] for row in rows), 2)

    def test_empty_results(self):
        output, summary = self.run_filter(min_length=10000)
        self.assertEqual(summary["交通相关帖子数"], 0)
        book = load_workbook(next(output.parent.glob("*.xlsx")))
        try:
            self.assertEqual(book["帖子"].max_row, 1)
            self.assertEqual(book["评论"].max_row, 1)
        finally:
            book.close()

    def test_unique_batches(self):
        first, _ = self.run_filter(export_excel=False)
        second, _ = self.run_filter(export_excel=False)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())

    def test_rules_and_counts(self):
        self.assertEqual(clean_text("<b>道路</b>&nbsp; 管制"), "道路 管制")
        for text in ("明星翻车，网友称演员在舞台上超速逆行引发争议。", "市区医院发生火灾，消防正在调查处置，无人员受伤。"):
            self.assertFalse(evaluate_text(text)[0])
        for value, expected in (("1.2万+", 12000), ("1,234", 1234), (0, 0), ("2亿", 200000000), ("--", None), (-1, None), (None, None)):
            self.assertEqual(parse_count(value), expected)

    def test_invalid_settings(self):
        for kwargs in ({"top_ratio": 0}, {"top_ratio": 2}, {"weights": (1, -1, 3)}, {"weights": (0, 0, 0)}, {"rho": 0}, {"gravity": -1}, {"min_heat": -1}):
            with self.assertRaises(ValueError):
                self.run_filter(**kwargs)

    def test_short_titles_and_broad_traffic_topics(self):
        for text in (
            "突发！渣土车与非机动车相撞", "两机车网红骑摩托撞车双双遇难",
            "昨天骑电摩的时候出了车祸", "三辆车相撞，最冤的那辆居然是停着的",
            "电动车被撞现场", "交通事故合集", "交通安全小知识来啦",
            "成都交警发布双节出行提示", "科目一，驾考宝典", "一分钟教你防御性驾驶",
        ):
            with self.subTest(text=text):
                self.assertTrue(evaluate_text(text)[0])
        for text in (
            "欢迎来到上海交通大学！校园开放日", "都市天际线：立交桥交通科普",
            "今天演员演技翻车，舞台上超速逆行", "小猫被撞上了，宠物医院正在救治",
            "成语填空：交通事故，交警提醒参与答题", "今天又是美好的一天",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_text(text)[0])

    def test_heat_time_units_decay_and_invalid_times(self):
        row = dict(liked_count="100", comments_count="20", shared_count="10",
                   create_time=1700000000, last_modify_ts=1700086400000)
        expected = 0.75 * (0.1365 * 100 + 0.2385 * 20 + 0.625 * 10) / 3 ** 1.8
        self.assertAlmostEqual(compute_heat(row)["heat_score"], expected)
        self.assertEqual(compute_heat(row), compute_heat({**row, "last_modify_ts": 1700086400}))
        self.assertEqual(compute_heat(row), compute_heat({**row, "create_time": 1700000000000}))
        self.assertEqual(compute_heat(row), compute_heat(row))
        self.assertLess(compute_heat({**row, "last_modify_ts": 1700172800000})["heat_score"], expected)
        for changes in ({"create_time": None}, {"last_modify_ts": None}, {"create_time": "nan"},
                        {"last_modify_ts": 1699999999}, {"create_time": 0}):
            self.assertIsNone(compute_heat({**row, **changes})["heat_score"])

    def test_zero_scores_are_not_hot_candidates(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE weibo_note SET liked_count='0',comments_count='0',shared_count='0'")
            conn.commit()
        _, summary = self.run_filter(export_excel=False)
        self.assertEqual(summary["热点帖子数"], 0)

    def test_ranking_rewards_recent_posts(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE weibo_note SET create_time=last_modify_ts/1000-86400*100 WHERE note_id='a'")
            conn.commit()
        output, _ = self.run_filter(export_excel=False)
        with closing(sqlite3.connect(output)) as conn:
            self.assertEqual(conn.execute("SELECT note_id FROM traffic_hot_note").fetchall(), [("b",)])


if __name__ == "__main__":
    unittest.main()
