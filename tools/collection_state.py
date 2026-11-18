"""按平台保存分页游标和补采参数，续采状态与原始数据分开存放。"""

import json

from tools.weibo_state import WeiboState


class CollectionState(WeiboState):
    def __init__(self, path):
        super().__init__(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS search_cursors (
                scope TEXT, keyword TEXT, payload TEXT, PRIMARY KEY(scope,keyword));
            CREATE TABLE IF NOT EXISTS task_context (
                item_key TEXT PRIMARY KEY, payload TEXT);
        """)
        self.conn.commit()

    def begin(self, keywords, search_type, start_page, max_notes, resume=True):
        scope = super().begin(keywords, search_type, start_page, max_notes, resume)
        for keyword in keywords:
            if self.progress(scope, keyword) == (start_page, 0, 0):
                self.conn.execute("DELETE FROM search_cursors WHERE scope=? AND keyword=?", (scope, keyword))
        self.conn.commit()
        return scope

    def cursor(self, scope, keyword):
        row = self.conn.execute("SELECT payload FROM search_cursors WHERE scope=? AND keyword=?", (scope, keyword)).fetchone()
        return json.loads(row[0]) if row else {}

    def checkpoint_page(self, scope, keyword, page, collected, completed, cursor):
        self.conn.execute("UPDATE search_progress SET next_page=?,collected=?,completed=? WHERE scope=? AND keyword=?",
                          (page, collected, int(completed), scope, keyword))
        self.conn.execute("INSERT OR REPLACE INTO search_cursors VALUES (?,?,?)",
                          (scope, keyword, json.dumps(cursor, ensure_ascii=False)))
        self.conn.commit()

    def save_context(self, key, context):
        self.conn.execute("INSERT OR REPLACE INTO task_context VALUES (?,?)", (key, json.dumps(context, ensure_ascii=False)))
        self.conn.commit()

    def context(self, key):
        row = self.conn.execute("SELECT payload FROM task_context WHERE item_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
