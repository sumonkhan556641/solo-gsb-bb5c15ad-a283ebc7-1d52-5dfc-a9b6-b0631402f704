"""保留策略评估接口。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends

from .. import db
from ..config import settings
from ..deps import get_db
from ..matching import Event, PolicyMatch, resolve
from ..retention import Period, add_period, parse_period
from ..schemas import ResolveResponse, RuleHit, EventIn

router = APIRouter(tags=["evaluate"])

_ACTION_ZH = {
    "delete": "删除",
    "anonymize": "匿名化",
    "archive": "归档",
    "legal_hold": "诉讼冻结",
}
_DIMENSION_ZH = {
    "event_type": "事件类型",
    "tenant": "租户",
    "sensitivity": "敏感级别",
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expires_for(match: PolicyMatch, occurred_at: datetime) -> tuple[datetime | None, Period | None]:
    """按单条命中规则自身的保留时长计算到期时间。"""
    if match.legal_hold or match.action == "legal_hold":
        return None, None
    if not match.retention_period:
        return None, None
    period, _ = parse_period(match.retention_period)
    if period is None or period.indefinite:
        return None, period
    return add_period(occurred_at, period), period


def _build_rule_hit(match: PolicyMatch, occurred_at: datetime, *, rank: int) -> RuleHit:
    expires_at, _period = _expires_for(match, occurred_at)
    detail = {
        key: f"{_DIMENSION_ZH[key]}：{dim.detail} [{dim.mode}]"
        for key, dim in match.dimensions.items()
    }
    return RuleHit(
        policy_id=match.policy_id,
        name=match.name,
        priority=match.priority,
        specificity=match.specificity,
        rank=rank,
        selected=(rank == 1),
        action=match.action,
        legal_hold=match.legal_hold,
        retention_period=(
            None if match.legal_hold or match.action == "legal_hold" else match.retention_period
        ),
        expires_at=expires_at,
        matched=True,
        match_detail=detail,
        reason=(
            f"匹配度 {match.specificity}，优先级 {match.priority}"
            + ("，无限期冻结" if expires_at is None and (match.legal_hold or match.action == "legal_hold") else "")
        ),
    )


def _build_unmatched(match: PolicyMatch) -> RuleHit:
    failed = [
        f"{_DIMENSION_ZH[key]}（{dim.detail}）"
        for key, dim in match.dimensions.items()
        if not dim.matched
    ]
    detail = {
        key: f"{_DIMENSION_ZH[key]}：{dim.detail} [{dim.mode}]"
        for key, dim in match.dimensions.items()
    }
    return RuleHit(
        policy_id=match.policy_id,
        name=match.name,
        priority=match.priority,
        specificity=match.specificity,
        rank=0,
        selected=False,
        action=match.action,
        legal_hold=match.legal_hold,
        retention_period=match.retention_period,
        expires_at=None,
        matched=False,
        match_detail=detail,
        reason="未命中：" + "；".join(failed),
    )


@router.post(
    "/evaluate",
    response_model=ResolveResponse,
    summary="评估事件的数据保留策略",
)
def evaluate_event(event_in: EventIn, conn: sqlite3.Connection = Depends(get_db)) -> ResolveResponse:
    occurred_at = _as_utc(event_in.occurred_at)
    event = Event(
        event_type=event_in.event_type.strip(),
        tenant=event_in.tenant.strip(),
        sensitivity=event_in.sensitivity,
    )

    # 匹配引擎需要属性访问，用 SimpleNamespace 包装行数据
    policies = [
        SimpleNamespace(**{**row, "conditions": SimpleNamespace(**row["conditions"])})
        for row in db.list_policies(conn, enabled_only=True)
    ]

    hits, misses = resolve(policies, event, settings)

    explanation: list[str] = []
    explanation.append(
        f"收到事件：类型={event.event_type}，租户={event.tenant}，"
        f"敏感级别={event.sensitivity}，发生时间(UTC)={occurred_at.isoformat()}"
    )

    chain: list[RuleHit]
    unmatched = [_build_unmatched(match) for match in misses]

    if hits:
        chain = [
            _build_rule_hit(match, occurred_at, rank=index + 1)
            for index, match in enumerate(hits)
        ]
        winner = hits[0]
        winner_hit = chain[0]
        if len(hits) > 1:
            runner_up = hits[1]
            explanation.append(
                f"共有 {len(hits)} 条策略命中，按“匹配度优先、其次优先级”排序；"
                f"头部规则 {winner.name!r}(id={winner.policy_id}) 的匹配度为 "
                f"{winner.specificity}、优先级 {winner.priority}，"
                f"高于次位 {runner_up.name!r}(匹配度 {runner_up.specificity}/优先级 {runner_up.priority})，"
                "因此生效。"
            )
        else:
            explanation.append(
                f"仅 1 条策略命中：{winner.name!r}(id={winner.policy_id})，直接生效。"
            )

        if winner_hit.legal_hold or winner.action == "legal_hold":
            expires_at = None
            explanation.append(
                f"生效动作是{_ACTION_ZH.get(winner.action, winner.action)}，数据无限期保留，"
                "不得自动删除或匿名化。"
            )
        else:
            expires_at = winner_hit.expires_at
            period, _ = parse_period(winner.retention_period or "")
            period_text = period.describe() if period else winner.retention_period
            explanation.append(
                f"生效动作：{_ACTION_ZH.get(winner.action, winner.action)}；"
                f"保留 {period_text}，到期时间(UTC)={expires_at.isoformat() if expires_at else '未知'}，"
                "到期后按动作处置。"
            )
        return ResolveResponse(
            event={
                "event_type": event.event_type,
                "tenant": event.tenant,
                "sensitivity": event.sensitivity,
                "occurred_at": occurred_at.isoformat(),
            },
            expires_at=expires_at,
            action=winner.action,
            legal_hold=winner_hit.legal_hold,
            retention_period=winner_hit.retention_period,
            rule_chain=chain,
            unmatched=unmatched,
            explanation=explanation,
            fallback_applied=False,
        )

    # 无任何命中：回退到服务默认保留期
    fallback_period, _ = parse_period(f"{settings.default_retention_days}d")
    assert fallback_period is not None
    expires_at = add_period(occurred_at, fallback_period)
    explanation.append(
        f"没有任何启用策略命中；回退到服务默认保留期 {settings.default_retention_days} 天，"
        f"到期时间(UTC)={expires_at.isoformat()}，到期后删除。"
    )
    return ResolveResponse(
        event={
            "event_type": event.event_type,
            "tenant": event.tenant,
            "sensitivity": event.sensitivity,
            "occurred_at": occurred_at.isoformat(),
        },
        expires_at=expires_at,
        action="delete",
        legal_hold=False,
        retention_period=fallback_period.canonical(),
        rule_chain=[],
        unmatched=unmatched,
        explanation=explanation,
        fallback_applied=True,
    )
