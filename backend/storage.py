"""单进程本地持久化；业务只使用 tasks / operations / events 三张表。"""
import json
import sqlite3
import uuid
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        # LangChain 的同步 ToolNode 可能在线程池执行工具；应用工具入口串行化。
        self.db = sqlite3.connect(Path(directory) / "business.sqlite", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (owner TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations
                (id TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events
                (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                 payload TEXT NOT NULL, at REAL NOT NULL, dedupe TEXT,
                 UNIQUE(owner, dedupe));
        """)

    def task(self, owner):
        row = self.db.execute("SELECT body FROM tasks WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_task(self, owner, body):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (owner, encode(body)))

    def operations(self, owner):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT body FROM operations WHERE owner=? ORDER BY rowid", (owner,))]

    def save_operation(self, owner, operation):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO operations VALUES (?, ?, ?)",
                            (operation["id"], owner, encode(operation)))

    def event(self, owner, kind, payload, at, dedupe=None):
        event_id = uuid.uuid4().hex
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)",
                                     (event_id, owner, kind, encode(payload), at, dedupe))
        return event_id if cursor.rowcount else None

    def has_event(self, owner, dedupe):
        return self.db.execute("SELECT 1 FROM events WHERE owner=? AND dedupe=?",
                               (owner, dedupe)).fetchone() is not None

    def events(self, owner, kind=None):
        query = "SELECT id,kind,payload,at FROM events WHERE owner=?"
        args = [owner]
        if kind:
            query += " AND kind=?"
            args.append(kind)
        return [dict(id=r[0], kind=r[1], payload=json.loads(r[2]), at=r[3])
                for r in self.db.execute(query + " ORDER BY rowid", args)]

    def close(self):
        self.db.close()
