"""FastAPI 依赖：请求级数据库连接。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = request.app.state.db
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
