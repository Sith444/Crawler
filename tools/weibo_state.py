"""保存微博搜索断点、全文缓存、评论补采状态和失败记录。"""

import hashlib
import json
from pathlib import Path
import sqlite3
import time


class WeiboState:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS search_progress (
                scope TEXT, keyword TEXT, next_page INTEGER, collected INTEGER,
                completed INTEGER DEFAULT 0, PRIMARY KEY(scope,keyword));
            CREATE TABLE IF NOT EXISTS post_cache (
                note_id TEXT PRIMARY KEY, content TEXT, detail_at REAL);
            CREATE TABLE IF NOT EXISTS comment_progress (
                note_id TEXT PRIMARY KEY, fetched_at REAL, requested_limit INTEGER);
            CREATE TABLE IF NOT EXISTS failures (
                kind TEXT, item_key TEXT, message TEXT, attempted_at REAL,
                PRIMARY KEY(kind,item_key));
            CREATE TABLE IF NOT EXISTS search_hits (
                note_id TEXT, keyword TEXT, PRIMARY KEY(note_id,keyword));
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def begin(self, keywords, search_type, start_page, max_notes, resume=True):
        payload = json.dumps([keywords, search_type, start_page, max_notes], ensure_ascii=False)
        scope = hashlib.sha256(payload.encode()).hexdigest()
        rows = self.conn.execute("SELECT completed FROM search_progress WHERE scope=?", (scope,)).fetchall()
        if not resume or (len(rows) == len(keywords) and all(row[0] for row in rows)):
            self.conn.execute("DELETE FROM search_progress WHERE scope=?", (scope,))
        for keyword in keywords:
            self.conn.execute("INSERT OR IGNORE INTO search_progress VALUES (?,?,?,0,0)", (scope, keyword, start_page))
        self.conn.commit()
        return scope

    def progress(self, scope, keyword):
        return self.conn.execute("SELECT next_page,collected,completed FROM search_progress WHERE scope=? AND keyword=?", (scope, keyword)).fetchone()

    def checkpoint(self, scope, keyword, page, collected, completed=False):
        self.conn.execute("UPDATE search_progress SET next_page=?,collected=?,completed=? WHERE scope=? AND keyword=?",
                          (page, collected, int(completed), scope, keyword))
        self.conn.commit()

    def hit(self, note_id, keyword):
        self.conn.execute("INSERT OR IGNORE INTO search_hits VALUES (?,?)", (str(note_id), keyword))
        self.conn.commit()

    def keyword(self, note_id):
        row = self.conn.execute("SELECT keyword FROM search_hits WHERE note_id=? ORDER BY keyword LIMIT 1", (str(note_id),)).fetchone()
        return row[0] if row else ""

    def cached_detail(self, note_id, refresh_hours):
        row = self.conn.execute("SELECT content,detail_at FROM post_cache WHERE note_id=?", (str(note_id),)).fetchone()
        if row and time.time() - row[1] < refresh_hours * 3600:
            return row[0]
        return None

    def save_detail(self, note_id, content):
        self.conn.execute("INSERT OR REPLACE INTO post_cache VALUES (?,?,?)", (str(note_id), content, time.time()))
        self.resolve("detail", note_id)
        self.conn.commit()

    def comments_fresh(self, note_id, refresh_hours, limit):
        row = self.conn.execute("SELECT fetched_at,requested_limit FROM comment_progress WHERE note_id=?", (str(note_id),)).fetchone()
        return bool(row and time.time() - row[0] < refresh_hours * 3600 and row[1] >= limit)

    def save_comments(self, note_id, limit):
        self.conn.execute("INSERT OR REPLACE INTO comment_progress VALUES (?,?,?)", (str(note_id), time.time(), limit))
        self.resolve("comments", note_id)
        self.conn.commit()

    def fail(self, kind, key, message):
        self.conn.execute("INSERT OR REPLACE INTO failures VALUES (?,?,?,?)", (kind, str(key), str(message)[:500], time.time()))
        self.conn.commit()

    def resolve(self, kind, key):
        self.conn.execute("DELETE FROM failures WHERE kind=? AND item_key=?", (kind, str(key)))
        self.conn.commit()

    def failed(self, kind):
        return [row[0] for row in self.conn.execute("SELECT item_key FROM failures WHERE kind=? ORDER BY attempted_at", (kind,))]
