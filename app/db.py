"""SQLite 连接管理与基础工具。

并发模型：所有写操作都在单连接上以 BEGIN IMMEDIATE 启动事务，
SQLite 写锁在全库范围内串行化写事务；busy_timeout 让后到者等待。
每个事务在事件流 events 中取得一个单调 seq，seq 即可核对的提交先后。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 时区感知的 ISO-8601 UTC 文本，例如 2026-11-26T08:00:00+00:00
ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def normalize_ts(value: str | datetime) -> str:
    """把任意 ISO-8601 时间（含 Z 后缀/带偏移）规范化为可比较的 UTC 文本。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def now_wall() -> str:
    return normalize_ts(datetime.now(timezone.utc))


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=30.0,
        isolation_level=None,  # 手工管理事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {30_000}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    row = conn.execute("SELECT now_iso FROM clock WHERE id = 1").fetchone()
    if row is None:
        initial = os.environ.get("LICENSE_EPOCH")
        conn.execute(
            "INSERT INTO clock(id, now_iso) VALUES (1, ?)",
            (normalize_ts(initial) if initial else now_wall(),),
        )


def get_clock(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT now_iso FROM clock WHERE id = 1").fetchone()[0]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即取写锁的事务上下文，锁竞争时等待；极端情况下有限重试。

    所有写操作都经此入口：BEGIN IMMEDIATE 在全库范围串行化写事务，
    busy_timeout 让后来者等待而非立即失败。重试安全，因为准入/续用/关闭
    都以 request_id/operation_id 做了幂等留痕。
    """
    last_error: sqlite3.OperationalError | None = None
    for _attempt in range(5):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:  # pragma: no cover - 极端锁竞争
            last_error = exc
            if "locked" not in str(exc).lower():
                raise
    else:  # pragma: no cover
        raise last_error
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def next_seq(conn: sqlite3.Connection, counter: str = "event") -> int:
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (counter,),
    )
    return conn.execute("SELECT value FROM counters WHERE name = ?", (counter,)).fetchone()[0]


def new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(10)}"
