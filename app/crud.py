"""策略的增删改查辅助函数（供路由与启动种子复用）。"""

from __future__ import annotations

import sqlite3

from .schemas import PolicyBase, PolicyOut

_COLUMNS = (
    "name",
    "description",
    "event_type",
    "tenant_id",
    "sensitivity",
    "priority",
    "retain_duration",
    "retain_unit",
    "keep_forever",
    "action",
    "enabled",
)


def _values(policy: PolicyBase) -> tuple:
    return (
        policy.name,
        policy.description or "",
        policy.event_type,
        policy.tenant_id,
        policy.sensitivity.value if policy.sensitivity else None,
        policy.priority,
        policy.retain_duration,
        policy.retain_unit.value if policy.retain_unit else None,
        1 if policy.keep_forever else 0,
        policy.action.value,
        1 if policy.enabled else 0,
    )


def row_to_out(row: sqlite3.Row) -> PolicyOut:
    return PolicyOut.model_validate(dict(row))


def name_exists(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> bool:
    sql = "SELECT 1 FROM retention_policies WHERE name = ?"
    params: list = [name]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone() is not None


def create_policy(conn: sqlite3.Connection, policy: PolicyBase) -> PolicyOut:
    placeholders = ", ".join("?" for _ in _COLUMNS)
    cur = conn.execute(
        f"INSERT INTO retention_policies ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        _values(policy),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM retention_policies WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return row_to_out(row)


def list_policies(conn: sqlite3.Connection) -> list[PolicyOut]:
    rows = conn.execute(
        "SELECT * FROM retention_policies ORDER BY priority ASC, id ASC"
    ).fetchall()
    return [row_to_out(r) for r in rows]


def get_policy(conn: sqlite3.Connection, policy_id: int) -> PolicyOut | None:
    row = conn.execute(
        "SELECT * FROM retention_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    return row_to_out(row) if row else None


def update_policy(
    conn: sqlite3.Connection, policy_id: int, policy: PolicyBase
) -> PolicyOut | None:
    sets = ", ".join(f"{c} = ?" for c in _COLUMNS if c != "name")
    sets += ", updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    values = _values(policy)
    # _values 中首项是 name，更新语句不需要它
    update_values = list(values[1:]) + [policy_id]
    cur = conn.execute(
        f"UPDATE retention_policies SET {sets} WHERE id = ?", update_values
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_policy(conn, policy_id)


def delete_policy(conn: sqlite3.Connection, policy_id: int) -> bool:
    cur = conn.execute("DELETE FROM retention_policies WHERE id = ?", (policy_id,))
    conn.commit()
    return cur.rowcount > 0


def count_policies(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM retention_policies").fetchone()[0]
