"""SQLite 数据访问层。

仅依赖 Python 标准库 ``sqlite3``：

* :func:`init_db` 在进程启动（FastAPI lifespan）时执行，自动创建目录、表结构；
* 使用 WAL 与外键约束；
* :data:`db` 为每个请求注入的连接依赖，结束时自动关闭。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_policies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT    NOT NULL DEFAULT '',
    -- 事件类型 / 租户 / 敏感级别为 NULL 表示该维度通配（匹配任意值）
    event_type      TEXT,
    tenant_id       TEXT,
    sensitivity     TEXT    CHECK (sensitivity IN ('low','medium','high','critical')),
    priority        INTEGER NOT NULL DEFAULT 100,
    retain_duration INTEGER,
    retain_unit     TEXT    CHECK (retain_unit IN ('days','months','years')),
    keep_forever    INTEGER NOT NULL DEFAULT 0,
    action          TEXT    NOT NULL DEFAULT 'expire'
                              CHECK (action IN ('expire','delete','anonymize','archive','legal_hold')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- 永久保留时时长可为空；否则时长与单位必须同时存在
    CHECK (
        keep_forever = 1
        OR (retain_duration IS NOT NULL AND retain_unit IS NOT NULL AND retain_duration > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_policies_lookup
    ON retention_policies (enabled, event_type, tenant_id, sensitivity);
"""


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # 内存库无法使用 WAL，避免测试时（:memory:）报错
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: str | None = None) -> None:
    """创建数据库目录、表结构与索引（幂等）。"""
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """短连接上下文：提交/回滚由调用方决定，退出即关闭。"""
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：为每个请求提供一个数据库连接。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
