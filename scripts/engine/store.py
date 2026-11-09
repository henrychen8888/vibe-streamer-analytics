"""SQLite 仓储层：隔离 SQL 方言，为将来切 PostgreSQL 留唯一改动点。

所有写操作走事务，并记审计日志；meta 表保存采集水位（watermark）与
数据新鲜度，供增量更新与 24h 缓存判定使用，避免「凭感觉重复采集」。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schema import Dataset, row_to, Blogger, Work, Comment, Product, FanSnapshot, to_dicts

TABLES = {
    "bloggers": ("id", "platform", "nickname", "industry", "followers", "followers_7d",
                 "followers_30d", "total_likes", "verified", "has_membership",
                 "member_price", "member_count", "member_works", "source", "collected_at"),
    "works": ("id", "blogger_id", "title", "publish_at", "duration_s", "views", "likes",
              "comments", "shares", "cover_style", "tag", "source"),
    "comments": ("id", "work_id", "blogger_id", "content", "likes", "created_at",
                 "replied", "sentiment", "source"),
    "products": ("id", "blogger_id", "name", "category", "price", "sold_display",
                 "rating", "source"),
    "fan_snapshots": ("blogger_id", "date", "followers", "source"),
}
CLASSES = {
    "bloggers": Blogger, "works": Work, "comments": Comment,
    "products": Product, "fan_snapshots": FanSnapshot,
}
PK = {"bloggers": "id", "works": "id", "comments": "id", "products": "id",
      "fan_snapshots": "blogger_id,date"}


def _now() -> str:
    # 本地时区带偏移，前端直接截取显示，避免 UTC 时间被误读
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    ddl = {
        "bloggers": """CREATE TABLE IF NOT EXISTS bloggers(
            id TEXT PRIMARY KEY, platform TEXT, nickname TEXT, industry TEXT,
            followers INTEGER, followers_7d INTEGER, followers_30d INTEGER,
            total_likes INTEGER, verified INTEGER, has_membership INTEGER,
            member_price REAL, member_count INTEGER, member_works INTEGER,
            source TEXT, collected_at TEXT)""",
        "works": """CREATE TABLE IF NOT EXISTS works(
            id TEXT PRIMARY KEY, blogger_id TEXT, title TEXT, publish_at TEXT,
            duration_s INTEGER, views INTEGER, likes INTEGER, comments INTEGER,
            shares INTEGER, cover_style TEXT, tag TEXT, source TEXT)""",
        "comments": """CREATE TABLE IF NOT EXISTS comments(
            id TEXT PRIMARY KEY, work_id TEXT, blogger_id TEXT, content TEXT,
            likes INTEGER, created_at TEXT, replied INTEGER, sentiment REAL, source TEXT)""",
        "products": """CREATE TABLE IF NOT EXISTS products(
            id TEXT PRIMARY KEY, blogger_id TEXT, name TEXT, category TEXT,
            price REAL, sold_display INTEGER, rating REAL, source TEXT)""",
        "fan_snapshots": """CREATE TABLE IF NOT EXISTS fan_snapshots(
            blogger_id TEXT, date TEXT, followers INTEGER, source TEXT,
            PRIMARY KEY(blogger_id, date))""",
        "meta": "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
        "audit_log": """CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, detail TEXT)""",
    }
    with conn:
        for sql in ddl.values():
            conn.execute(sql)
        for t in ("works", "comments", "products"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_blogger ON {t}(blogger_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_work ON comments(work_id)")


def audit(conn: sqlite3.Connection, action: str, detail: str = "") -> None:
    with conn:
        conn.execute("INSERT INTO audit_log(ts,action,detail) VALUES(?,?,?)", (_now(), action, detail))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def save_dataset(conn: sqlite3.Connection, ds: Dataset, source: str = "unknown") -> dict:
    """全量替换（演示/导入场景）。增量更新请走 upsert_* 并配合 watermark。"""
    init_schema(conn)
    payload = {"bloggers": ds.bloggers, "works": ds.works, "comments": ds.comments,
               "products": ds.products, "fan_snapshots": ds.fan_snapshots}
    with conn:
        for table, rows in payload.items():
            conn.execute(f"DELETE FROM {table}")
            cols = TABLES[table]
            sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
            conn.executemany(sql, [tuple(getattr(r, c) for c in cols) for r in rows])
        conn.execute("INSERT INTO audit_log(ts,action,detail) VALUES(?,?,?)",
                     (_now(), "save_dataset", f"source={source}; counts={ds.counts()}"))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("last_ingest_at", _now()))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("last_source", source))
    return ds.counts()


def upsert_works(conn: sqlite3.Connection, works: list[Work]) -> int:
    """增量更新示例：按 id 去重，仅写入新增作品（真实场景再叠加 updated_at 水位）。"""
    cols = TABLES["works"]
    sql = f"INSERT OR IGNORE INTO works({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    with conn:
        cur = conn.executemany(sql, [tuple(getattr(r, c) for c in cols) for r in works])
        conn.execute("INSERT INTO audit_log(ts,action,detail) VALUES(?,?,?)",
                     (_now(), "upsert_works", f"incoming={len(works)}; inserted={cur.rowcount}"))
    return cur.rowcount


def load_dataset(conn: sqlite3.Connection) -> Dataset:
    init_schema(conn)
    ds = Dataset()
    for table, cls in CLASSES.items():
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        setattr(ds, table, [row_to(cls, dict(r)) for r in rows])
    return ds


def freshness(conn: sqlite3.Connection) -> dict:
    last = get_meta(conn, "last_ingest_at")
    return {"last_ingest_at": last or None,
            "last_source": get_meta(conn, "last_source"),
            "watermark": get_meta(conn, "watermark"),
            "cache_valid": bool(last)}
