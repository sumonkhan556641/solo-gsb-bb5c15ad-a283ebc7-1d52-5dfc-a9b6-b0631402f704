"""内置示例策略：首次启动且策略表为空时写入，便于直接体验评估接口。"""

from __future__ import annotations

import sqlite3

from .schemas import PolicyCreate
from .crud import count_policies, create_policy

SEED_POLICIES: list[PolicyCreate] = [
    PolicyCreate(
        name="default-30-days",
        description="兜底策略：未命中更具体规则的事件默认保留 30 天",
        event_type=None,
        tenant_id=None,
        sensitivity=None,
        priority=1000,
        retain_duration=30,
        retain_unit="days",
        action="delete",
    ),
    PolicyCreate(
        name="pci-audit-high",
        description="PCI 审计类高敏感事件保留 1 年，到期匿名化",
        event_type="audit",
        tenant_id=None,
        sensitivity="high",
        priority=100,
        retain_duration=1,
        retain_unit="years",
        action="anonymize",
    ),
    PolicyCreate(
        name="tenant-a-login-90d",
        description="tenant-a 的登录事件保留 90 天",
        event_type="login",
        tenant_id="tenant-a",
        sensitivity=None,
        priority=50,
        retain_duration=90,
        retain_unit="days",
        action="delete",
    ),
    PolicyCreate(
        name="legal-hold-critical",
        description="最高敏感级别事件进入法律保全，永久保留",
        event_type=None,
        tenant_id=None,
        sensitivity="critical",
        priority=10,
        keep_forever=True,
        action="legal_hold",
    ),
]


def seed_if_empty(conn: sqlite3.Connection) -> int:
    """策略表为空时写入示例策略，返回写入条数。"""
    if count_policies(conn) > 0:
        return 0
    for policy in SEED_POLICIES:
        create_policy(conn, policy)
    return len(SEED_POLICIES)
