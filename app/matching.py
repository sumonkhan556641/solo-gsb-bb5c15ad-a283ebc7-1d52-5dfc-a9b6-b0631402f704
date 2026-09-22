"""策略匹配与裁决。

匹配度（specificity）计算：
- 三个维度命中时按“精确值命中”计分，通配维度计 0 分；
- min_sensitivity 阈值视为半精确条件，命中时给半权；
- 权重 event_type > tenant > sensitivity，事件类型是最细的区分维度。
排序规则（越靠前越先生效）：匹配度降序 → priority 降序 → id 升序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings

# 维度权重设计：任何两个非空维度的组合分都高于任何单一维度，
# 即“租户+敏感级别约束”比单纯“事件类型约束”更具体。
# 单维顺序：事件类型 > 租户 > 敏感级别集合 > 敏感阈值
WEIGHT_EVENT_TYPE = 10
WEIGHT_TENANT = 10
WEIGHT_SENSITIVITY = 5
WEIGHT_MIN_SENSITIVITY = 5


@dataclass
class Event:
    event_type: str
    tenant: str
    sensitivity: str  # 已归一化为小写


@dataclass
class DimensionResult:
    matched: bool
    mode: str  # exact / any / threshold / wildcard / no-rank
    detail: str


@dataclass
class PolicyMatch:
    policy_id: int
    name: str
    priority: int
    action: str
    legal_hold: bool
    retention_period: str | None
    specificity: int
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return all(dim.matched for dim in self.dimensions.values())


def _match_identifier(values: list[str], actual: str) -> tuple[bool, str, str]:
    """返回 (是否命中, 模式, 解释)。"""
    if not values or values == ["*"]:
        return True, "wildcard", "未限定（通配）"
    if actual in values:
        mode = "exact" if len(values) == 1 else "any"
        candidates = "、".join(values)
        return True, mode, f"命中 {actual!r}（候选：{candidates}）"
    return False, "miss", f"未命中：事件值 {actual!r} 不在候选（{'、'.join(values)}）中"


def evaluate_policy(policy: object, event: Event, settings: Settings) -> PolicyMatch:
    """评估单条策略对事件的逐维度匹配情况。"""
    conditions = policy.conditions
    result = PolicyMatch(
        policy_id=policy.id,
        name=policy.name,
        priority=policy.priority,
        action=policy.action,
        legal_hold=policy.legal_hold,
        retention_period=policy.retention_period,
        specificity=0,
    )

    matched, mode, detail = _match_identifier(conditions.event_type, event.event_type)
    result.dimensions["event_type"] = DimensionResult(matched, mode, detail)
    if matched and mode != "wildcard":
        result.specificity += WEIGHT_EVENT_TYPE

    matched, mode, detail = _match_identifier(conditions.tenant, event.tenant)
    result.dimensions["tenant"] = DimensionResult(matched, mode, detail)
    if matched and mode != "wildcard":
        result.specificity += WEIGHT_TENANT

    rank = settings.sensitivity_rank
    matched, mode, detail = _match_sensitivity(
        conditions.sensitivity, conditions.min_sensitivity, event.sensitivity, rank
    )
    result.dimensions["sensitivity"] = DimensionResult(matched, mode, detail)
    if matched:
        if conditions.sensitivity:
            result.specificity += WEIGHT_SENSITIVITY
        if conditions.min_sensitivity:
            result.specificity += WEIGHT_MIN_SENSITIVITY

    return result


def _match_sensitivity(
    allowed: list[str],
    minimum: str | None,
    actual: str,
    rank: dict[str, int],
) -> tuple[bool, str, str]:
    if not allowed and not minimum:
        return True, "wildcard", "未限定敏感级别（通配）"

    clauses: list[bool] = []
    descriptions: list[str] = []
    if allowed:
        in_list = actual in allowed
        clauses.append(in_list)
        descriptions.append(
            f"级别 {actual!r} {'属于' if in_list else '不属于'} 集合（{'、'.join(allowed)}）"
        )
    if minimum:
        if minimum not in rank:
            # 策略里出现了未配置的敏感级别名：永远无法匹配
            return False, "no-rank", f"策略阈值 {minimum!r} 未在服务敏感级别配置中定义"
        if actual not in rank:
            return False, "no-rank", f"事件敏感级别 {actual!r} 未在服务配置中定义"
        threshold_ok = rank[actual] >= rank[minimum]
        clauses.append(threshold_ok)
        descriptions.append(
            f"{actual!r} {'>=' if threshold_ok else '<'} 阈值 {minimum!r}"
        )

    matched = all(clauses)
    mode_parts = []
    if allowed:
        mode_parts.append("any" if len(allowed) > 1 else "exact")
    if minimum:
        mode_parts.append("threshold")
    mode = "+".join(mode_parts)
    return matched, mode, "；".join(descriptions)


def sort_key(match: PolicyMatch) -> tuple[int, int, int]:
    return (-match.specificity, -match.priority, match.policy_id)


def resolve(
    policies: list[object], event: Event, settings: Settings
) -> tuple[list[PolicyMatch], list[PolicyMatch]]:
    """评估全部策略，返回 (命中链, 未命中列表)。

    命中链按匹配度/优先级排序，首条为最终生效规则；未命中列表保持稳定顺序。
    """
    hits: list[PolicyMatch] = []
    misses: list[PolicyMatch] = []
    for policy in policies:
        match = evaluate_policy(policy, event, settings)
        (hits if match.matched else misses).append(match)
    hits.sort(key=sort_key)
    return hits, misses
