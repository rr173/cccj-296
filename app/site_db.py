"""站点本地连接管理：独立 SQLite 文件，与中心库物理隔离。

复用中心的事务/时钟/序号约定（BEGIN IMMEDIATE 全库写锁串行化），
但加载站点专用 schema，且不包含任何中心表。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import db
from .db import now_wall, normalize_ts

SITE_SCHEMA_PATH = Path(__file__).with_name("site_schema.sql")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SITE_SCHEMA_PATH.read_text(encoding="utf-8"))
    if conn.execute("SELECT now_iso FROM clock WHERE id = 1").fetchone() is None:
        initial = os.environ.get("SITE_EPOCH")
        conn.execute(
            "INSERT INTO clock(id, now_iso) VALUES (1, ?)",
            (normalize_ts(initial) if initial else now_wall(),),
        )


def get_clock(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT now_iso FROM clock WHERE id = 1").fetchone()[0]


def next_seq(conn: sqlite3.Connection) -> int:
    return db.next_seq(conn, counter="site_event")


def transaction(conn: sqlite3.Connection):
    return db.transaction(conn)
