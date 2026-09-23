"""核心评估引擎：策略匹配、匹配度打分、优先级解析、到期时间计算与解释生成。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta

from .schemas import (
    EvaluationResponse,
    EventIn,
    MatchedRule,
    RetentionAction,
    RetentionUnit,
    Specificity,
)

# 参与匹配的三个维度（顺序同时用于解释展示）
DIMENSIONS = ("event_type", "tenant_id", "sensitivity")
_DIMENSION_LABELS = {
    "event_type": "事件类型",
    "tenant_id": "租户",
    "sensitivity": "敏感级别",
}


def _as_utc(dt: datetime) -> datetime:
    """朴素时间按 UTC 处理；带时区时间统一转换为 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_expiry(
    occurred_at: datetime, duration: int | None, unit: str | None, keep_forever: bool
) -> datetime | None:
    """根据事件发生时间与保留时长计算到期时间（UTC）。

    * ``keep_forever`` 为真时返回 ``None``（不过期，如法律保全）；
    * 月/年使用 :class:`relativedelta` 处理日历长度差异，天使用自然日。
    """
    if keep_forever or duration is None or unit is None:
        return None
    base = _as_utc(occurred_at)
    if unit == RetentionUnit.DAYS:
        from datetime import timedelta

        return base + timedelta(days=duration)
    if unit == RetentionUnit.MONTHS:
        return base + relativedelta(months=duration)
    if unit == RetentionUnit.YEARS:
        return base + relativedelta(years=duration)
    raise ValueError(f"不支持的保留单位: {unit}")


def _dimension_map(row: sqlite3.Row) -> dict[str, str | None]:
    return {dim: row[dim] for dim in DIMENSIONS}


def _matches(row: sqlite3.Row, event: EventIn) -> bool:
    """策略的每个非通配维度必须与事件精确相等。"""
    if row["event_type"] is not None and row["event_type"] != event.event_type:
        return False
    if row["tenant_id"] is not None and row["tenant_id"] != event.tenant_id:
        return False
    if row["sensitivity"] is not None and row["sensitivity"] != event.sensitivity:
        return False
    return True


def _matched_dimensions(row: sqlite3.Row, event: EventIn) -> tuple[dict[str, str], int]:
    """返回各维度匹配方式与匹配度得分（精确匹配的维度数，0-3）。"""
    result: dict[str, str] = {}
    score = 0
    for dim in DIMENSIONS:
        if row[dim] is None:
            result[dim] = Specificity.WILDCARD.name.lower()
        else:
            result[dim] = Specificity.EXACT.name.lower()
            score += 1
    return result, score


def _rule_explanation(
    row: sqlite3.Row, event: EventIn, dims: dict[str, str], score: int
) -> str:
    exact = [
        f"{_DIMENSION_LABELS[d]}={getattr(event, d)}"
        for d in DIMENSIONS
        if dims[d] == "exact"
    ]
    wildcard = [
        f"{_DIMENSION_LABELS[d]}=*" for d in DIMENSIONS if dims[d] == "wildcard"
    ]
    parts = f"精确匹配 {score}/3 个维度"
    if exact:
        parts += "（" + "、".join(exact) + "）"
    if wildcard:
        parts += "；通配维度：" + "、".join(wildcard)
    if bool(row["keep_forever"]):
        parts += "；处置：永久保留（不过期）"
    else:
        parts += (
            f"；保留 {row['retain_duration']} {row['retain_unit']}"
            f"，到期动作 {row['action']}"
        )
    parts += f"；优先级 {row['priority']}。"
    return parts


def _decision_explanation(
    event: EventIn,
    chain: list[tuple[sqlite3.Row, dict[str, str], int]],
    winner: sqlite3.Row | None,
    expires_at: datetime | None,
) -> str:
    if winner is None:
        return (
            f"没有任何启用的策略匹配事件 event_type={event.event_type}、"
            f"tenant_id={event.tenant_id}、sensitivity={event.sensitivity}，"
            "因此无法解析保留策略；请调整事件或新建覆盖该组合的策略。"
        )
    head = (
        f"共命中 {len(chain)} 条策略。按“精确匹配维度数（匹配度）降序、"
        f"优先级 priority 升序、id 升序”解析，规则 `{winner['name']}` "
        f"(id={winner['id']}) 胜出。"
    )
    if bool(winner["keep_forever"]):
        tail = "该策略标记为永久保留，数据不设置到期时间。"
    else:
        tail = (
            f"事件发生于 {_as_utc(event.occurred_at).isoformat()}，"
            f"保留 {winner['retain_duration']} {winner['retain_unit']} 后，"
            f"到期时间为 {expires_at.isoformat()}，"
            f"到期动作：{winner['action']}。"
        )
    return head + tail


def evaluate(event: EventIn, conn: sqlite3.Connection) -> EvaluationResponse:
    """评估单个事件，返回生效策略、到期时间、完整规则链与解释。"""
    rows = conn.execute(
        "SELECT * FROM retention_policies WHERE enabled = 1"
    ).fetchall()

    scored: list[tuple[sqlite3.Row, dict[str, str], int]] = []
    for row in rows:
        if not _matches(row, event):
            continue
        dims, score = _matched_dimensions(row, event)
        scored.append((row, dims, score))

    # 排序：匹配度（精确维度数）降序 -> 优先级升序 -> id 升序（确定性）
    scored.sort(key=lambda item: (-item[2], item[0]["priority"], item[0]["id"]))

    occurred_utc = _as_utc(event.occurred_at)
    rule_chain: list[MatchedRule] = []
    winner: sqlite3.Row | None = None
    expires_at: datetime | None = None

    for rank, (row, dims, score) in enumerate(scored, start=1):
        if rank == 1:
            winner = row
            expires_at = compute_expiry(
                occurred_utc,
                row["retain_duration"],
                row["retain_unit"],
                bool(row["keep_forever"]),
            )
        rule_chain.append(
            MatchedRule(
                rank=rank,
                policy_id=row["id"],
                name=row["name"],
                priority=row["priority"],
                specificity_score=score,
                matched_dimensions=dims,
                retain_duration=row["retain_duration"],
                retain_unit=row["retain_unit"],
                keep_forever=bool(row["keep_forever"]),
                action=RetentionAction(row["action"]),
                explanation=_rule_explanation(row, event, dims, score),
            )
        )

    if winner is None:
        return EvaluationResponse(
            event={
                "event_type": event.event_type,
                "tenant_id": event.tenant_id,
                "sensitivity": event.sensitivity,
                "occurred_at": occurred_utc.isoformat(),
            },
            matched=False,
            winning_policy_id=None,
            winning_policy_name=None,
            expires_at=None,
            retain_period=None,
            action=None,
            keep_forever=False,
            match_count=0,
            rule_chain=[],
            decision_explanation=_decision_explanation(event, [], None, None),
        )

    retain_period = (
        None
        if bool(winner["keep_forever"])
        else f"{winner['retain_duration']} {winner['retain_unit']}"
    )
    return EvaluationResponse(
        event={
            "event_type": event.event_type,
            "tenant_id": event.tenant_id,
            "sensitivity": event.sensitivity,
            "occurred_at": occurred_utc.isoformat(),
        },
        matched=True,
        winning_policy_id=winner["id"],
        winning_policy_name=winner["name"],
        expires_at=expires_at,
        retain_period=retain_period,
        action=RetentionAction(winner["action"]),
        keep_forever=bool(winner["keep_forever"]),
        match_count=len(scored),
        rule_chain=rule_chain,
        decision_explanation=_decision_explanation(event, scored, winner, expires_at),
    )
