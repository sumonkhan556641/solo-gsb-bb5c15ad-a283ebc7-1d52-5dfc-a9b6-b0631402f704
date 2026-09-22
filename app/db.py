"""SQLite 持久化层：建表、种子数据与策略 CRUD。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_policies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    event_types     TEXT NOT NULL DEFAULT '[]',
    tenants         TEXT NOT NULL DEFAULT '[]',
    sensitivities   TEXT NOT NULL DEFAULT '[]',
    min_sensitivity TEXT,
    action          TEXT NOT NULL DEFAULT 'delete',
    retention_period TEXT,
    legal_hold      INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policies_enabled ON retention_policies(enabled);
"""


def _conditions(event_type=None, tenant=None, sensitivity=None, min_sensitivity=None) -> dict[str, Any]:
    """种子数据用的嵌套 conditions 构造器，形状与 PolicyCreate 一致。"""
    return {
        "event_type": event_type or [],
        "tenant": tenant or [],
        "sensitivity": sensitivity or [],
        "min_sensitivity": min_sensitivity,
    }


# 首次启动写入的演示策略；保留期已使用标准形式。
_SEED_POLICIES: list[dict[str, Any]] = [
    {
        "name": "安全审计日志-90天",
        "description": "所有租户的安全事件保留 90 天后删除",
        "conditions": _conditions(event_type=["security_alert"]),
        "action": "delete",
        "retention_period": "P90D",
        "legal_hold": False,
        "priority": 100,
    },
    {
        "name": "金融租户交易记录-7年",
        "description": "fin 租户的交易事件按合规要求归档保留 7 年",
        "conditions": _conditions(event_type=["transaction"], tenant=["fin"]),
        "action": "archive",
        "retention_period": "P7Y",
        "legal_hold": False,
        "priority": 200,
    },
    {
        "name": "机密以上用户数据-1年",
        "description": "confidential 及以上敏感级别的用户相关事件保留 1 年后删除",
        "conditions": _conditions(min_sensitivity="confidential"),
        "action": "delete",
        "retention_period": "P1Y",
        "legal_hold": False,
        "priority": 100,
    },
    {
        "name": "诉讼冻结-无限期",
        "description": "legal 租户任何机密以上事件进入诉讼冻结，不得自动删除",
        "conditions": _conditions(tenant=["legal"], min_sensitivity="confidential"),
        "action": "legal_hold",
        "retention_period": None,
        "legal_hold": True,
        "priority": 500,
    },
    {
        "name": "默认保留-30天",
        "description": "兜底规则：未被其他策略覆盖的事件保留 30 天",
        "conditions": _conditions(),
        "action": "delete",
        "retention_period": "P30D",
        "priority": 0,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(database_path: str | None = None) -> sqlite3.Connection:
    path = database_path or settings.database_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表并在空库时写入种子策略。"""
    conn.executescript(_SCHEMA)
    if settings.seed_sample_policies:
        count = conn.execute("SELECT COUNT(*) FROM retention_policies").fetchone()[0]
        if count == 0:
            for policy in _SEED_POLICIES:
                create_policy(conn, policy, commit=False)
    conn.commit()


def _row_to_policy(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "conditions": {
            "event_type": json.loads(row["event_types"]),
            "tenant": json.loads(row["tenants"]),
            "sensitivity": json.loads(row["sensitivities"]),
            "min_sensitivity": row["min_sensitivity"],
        },
        "action": row["action"],
        "retention_period": row["retention_period"],
        "legal_hold": bool(row["legal_hold"]),
        "priority": row["priority"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_policies(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM retention_policies"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return [_row_to_policy(row) for row in conn.execute(sql)]


def get_policy(conn: sqlite3.Connection, policy_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM retention_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    return _row_to_policy(row) if row else None


def get_policy_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM retention_policies WHERE name = ?", (name,)
    ).fetchone()
    return _row_to_policy(row) if row else None


def create_policy(conn: sqlite3.Connection, payload: dict[str, Any], *, commit: bool = True) -> dict[str, Any]:
    """payload 接受 PolicyCreate 形态：含嵌套 conditions。"""
    conditions = payload.get("conditions") or {}
    timestamp = _now()
    cursor = conn.execute(
        """
        INSERT INTO retention_policies (
            name, description, event_types, tenants, sensitivities,
            min_sensitivity, action, retention_period, legal_hold,
            priority, enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload.get("description", ""),
            json.dumps(conditions.get("event_type", []), ensure_ascii=False),
            json.dumps(conditions.get("tenant", []), ensure_ascii=False),
            json.dumps(conditions.get("sensitivity", []), ensure_ascii=False),
            conditions.get("min_sensitivity"),
            payload.get("action", "delete"),
            payload.get("retention_period"),
            1 if payload.get("legal_hold", False) else 0,
            payload.get("priority", 100),
            1 if payload.get("enabled", True) else 0,
            timestamp,
            timestamp,
        ),
    )
    if commit:
        conn.commit()
    return get_policy(conn, cursor.lastrowid)  # type: ignore[arg-type]


def update_policy(
    conn: sqlite3.Connection,
    policy_id: int,
    fields: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    """按 fields 更新策略。

    fields 应为 PATCH 请求中“显式传入”的字段（exclude_unset 结果），
    因此显式传入的 None（如把 retention_period 置空）会被尊重。
    """
    current = get_policy(conn, policy_id)
    if current is None:
        return None

    conditions = fields.get("conditions")
    if conditions is not None:
        merged_conditions = current["conditions"] | conditions
    else:
        merged_conditions = dict(current["conditions"])

    def pick(key: str, default: Any) -> Any:
        return fields[key] if key in fields else default

    conn.execute(
        """
        UPDATE retention_policies SET
            name = ?, description = ?, event_types = ?, tenants = ?,
            sensitivities = ?, min_sensitivity = ?, action = ?,
            retention_period = ?, legal_hold = ?, priority = ?,
            enabled = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            pick("name", current["name"]),
            pick("description", current["description"]),
            json.dumps(merged_conditions["event_type"], ensure_ascii=False),
            json.dumps(merged_conditions["tenant"], ensure_ascii=False),
            json.dumps(merged_conditions["sensitivity"], ensure_ascii=False),
            merged_conditions["min_sensitivity"],
            pick("action", current["action"]),
            (
                fields["retention_period"]
                if "retention_period" in fields
                else current["retention_period"]
            ),
            int(bool(pick("legal_hold", current["legal_hold"]))),
            pick("priority", current["priority"]),
            int(bool(pick("enabled", current["enabled"]))),
            _now(),
            policy_id,
        ),
    )
    if commit:
        conn.commit()
    return get_policy(conn, policy_id)


def delete_policy(conn: sqlite3.Connection, policy_id: int) -> bool:
    cursor = conn.execute("DELETE FROM retention_policies WHERE id = ?", (policy_id,))
    conn.commit()
    return cursor.rowcount > 0


def enabled_policy_dicts(conn: sqlite3.Connection) -> Iterable[dict[str, Any]]:
    return list_policies(conn, enabled_only=True)
